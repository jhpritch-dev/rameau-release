"""Phase B request-path wiring for the routing cache.

This module exists to keep the edit to main.py -- which is PROTECTED -- as small as it can be.
Everything decidable lives here, where it is unit-testable and freely editable; main.py gets
one import and one substituted call.

WHAT PROMOTION ACTUALLY COSTS. In Phase A the embed ran in a detached task AFTER the classifier
had already decided, so it never touched request latency. Serving means moving that embed onto
the synchronous path, ahead of classification, and awaiting it. The shadow data cannot tell us
what that costs because the shadow never paid it. That is why `timeout_s` exists and why it is
small: a cold embed must degrade into a normal classification, not into a slow request.

The arithmetic that justifies it: NPU classification runs a 7-day mean of 3,356 ms; a warm embed
on Hearth :11434 measures 60-90 ms.

FAIL CLOSED, ALWAYS. Disabled, hinted, admission-denied, embed timeout, embed error, cache
error, out-of-allowlist band, or any lookup miss -- every one of them returns a decline and the
caller classifies exactly as it does today. No path here may raise into the request.
"""
import asyncio
import logging

import routing_cache

log = logging.getLogger("bifrost.routing_cache.serve_path")

# A cold embed must never become a slow request. 250 ms is roughly 3x a warm embed and roughly
# 1/13th of the classifier it replaces, so the worst case of a miss-after-timeout is still far
# cheaper than the thing being avoided.
DEFAULT_SERVE_TIMEOUT_S = 0.25

# Marker written into the routing telemetry dict. main.py's shadow-write guard tests this: a
# request answered FROM the cache must not be written BACK to the cache. Doing so would feed
# the cache its own output as though it were authoritative, and its errors would compound with
# nothing to correct them. Only a genuine classification is authoritative enough to store.
ROUTING_SOURCE = "routing_cache"


def routing_td(decision):
    """Routing telemetry for a cache-served request.

    Deliberately carries the SAME KEYS as the rule-fallback dict built in main.py, so that
    every downstream consumer sees the shape it already expects and a served request cannot
    raise a KeyError somewhere far from here.
    """
    return {
        "routing_source": ROUTING_SOURCE,
        "rule_pred": None,
        "npu_pred": None,
        "npu_conf": None,
        "npu_latency_ms": None,
        "fallback_reason": None,
        "agreed": None,
        "cache_similarity": getattr(decision, "similarity", None),
        "cache_confidence": getattr(decision, "confidence", None),
    }


def classification_from(decision):
    """Build a ClassificationResult from a cache hit.

    Imported lazily so this module stays importable (and testable) without the router's
    dependency graph.

    hint_override is False by design: a hinted request never reaches the cache at all, so a
    served result cannot be hint-derived, and claiming otherwise would corrupt the very guard
    that keeps hinted requests out.
    """
    from classifier import ClassificationResult, ComplexityBand

    band = decision.band
    if not isinstance(band, ComplexityBand):
        band = ComplexityBand(band)
    return ClassificationResult(
        band=band,
        confidence=float(decision.confidence if decision.confidence is not None else 0.0),
        reasoning="routing cache hit (similarity %.4f, engine %s)"
                  % (decision.similarity or 0.0, ROUTING_SOURCE),
        scores={},
        hint_override=False,
    )


def status_line(loaded_entries):
    """The startup line describing what the cache will actually DO this boot.

    Computed, not fixed. The previous line was the literal string "(shadow mode; serves
    nothing)", which stayed in the logs for hours after the serve path went live and was
    serving -- precisely the kind of stale reassurance someone reads during an incident and
    believes. main.py holds a RoutingCache INSTANCE under the name `routing_cache`, so it
    cannot reach the module-level switch itself; this helper does it on its behalf and keeps
    the protected file's diff to one line.
    """
    if not routing_cache.serve_enabled():
        return ("loaded entries=%d cap=%d (shadow mode; serves nothing -- set %s to serve)"
                % (loaded_entries, routing_cache.DEFAULT_MAX_SIZE, routing_cache.SERVE_ENV_VAR))
    return ("loaded entries=%d cap=%d SERVING bands=%s timeout=%.0fms"
            % (loaded_entries, routing_cache.DEFAULT_MAX_SIZE,
               ",".join(sorted(routing_cache.serve_bands())),
               DEFAULT_SERVE_TIMEOUT_S * 1000))


async def try_serve(prompt, cache, embed_fn, config_version,
                    hint_present=False, admit=None,
                    timeout_s=DEFAULT_SERVE_TIMEOUT_S, env=None):
    """Attempt to answer this request from the cache. Returns a routing_cache.ServeDecision.

    Order matters. The enabled check and the hint check come BEFORE the embed, so a disabled or
    hinted request costs nothing at all -- not an embed, not an admission slot. Paying for a
    lookup we are contractually forbidden to use is exactly the Phase A failure this work is
    meant to end.
    """
    if not routing_cache.serve_enabled(env):
        return routing_cache.ServeDecision(serve=False, reason="disabled")
    if hint_present:
        return routing_cache.ServeDecision(serve=False, reason="hint_bypass")

    try:
        if admit is None:
            return await _embed_and_serve(prompt, cache, embed_fn, config_version,
                                          timeout_s, env)
        # The admission breaker matters MORE here than in the shadow. This is one of the two
        # paths that can pin ollama_llama_server.exe on Hearth, and it is now in front of
        # every request rather than behind them.
        async with admit("routing_cache_serve") as slot:
            if not getattr(slot, "admitted", False):
                return routing_cache.ServeDecision(serve=False, reason="admission_denied")
            try:
                return await _embed_and_serve(prompt, cache, embed_fn, config_version,
                                              timeout_s, env)
            except Exception as exc:                                      # noqa: BLE001
                _report(slot, exc)
                raise
    except asyncio.TimeoutError:
        log.debug("routing_cache serve: embed exceeded %.3fs budget; classifying instead",
                  timeout_s)
        return routing_cache.ServeDecision(serve=False, reason="embed_timeout")
    except Exception as exc:                                              # noqa: BLE001
        # Nothing in the cache path is allowed to fail a request.
        log.warning("routing_cache serve declined (%s: %s); classifying instead",
                    type(exc).__name__, exc)
        return routing_cache.ServeDecision(serve=False, reason="embed_error")


async def _embed_and_serve(prompt, cache, embed_fn, config_version, timeout_s, env):
    vector = await asyncio.wait_for(embed_fn(prompt), timeout=timeout_s)
    # hint_present is already handled above; passing False here keeps the single source of
    # truth for that rule at the top of try_serve rather than in two places.
    return routing_cache.serve(cache, vector, config_version, hint_present=False, env=env)


def _report(slot, exc):
    """Tell the breaker before swallowing. A breaker cannot learn from an exception it is
    never shown -- the same reasoning as the Phase A shadow path."""
    try:
        slot.failed("%s" % type(exc).__name__)
    except Exception:                                                     # noqa: BLE001
        pass


async def serve_or_classify(prompt, file_count, hint, classify_fn, cache, embed_fn,
                            config_version_fn, admit=None,
                            timeout_s=DEFAULT_SERVE_TIMEOUT_S, env=None):
    """Serve from cache when allowed, otherwise classify. Returns (classification, routing_td).

    This is the single call main.py substitutes for its classifier invocation, which keeps the
    protected diff to one statement. `config_version_fn` is a callable rather than a value so
    that the hash is never computed on the disabled path. Exceptions from `classify_fn` propagate untouched, so the
    caller's existing error handling -- the 503 on NPU-unavailable in live mode, the rule
    fallback in shadow mode -- keeps working exactly as written.
    """
    # Short-circuit before ANY work when the switch is off, including the config-version
    # hash. With the feature disabled this function must cost nothing measurable, or the
    # negative control stops being a real control.
    if not routing_cache.serve_enabled(env):
        return await classify_fn(prompt, file_count, hint)

    decision = await try_serve(prompt=prompt, cache=cache, embed_fn=embed_fn,
                               config_version=config_version_fn(),
                               hint_present=bool(hint), admit=admit,
                               timeout_s=timeout_s, env=env)
    if decision.serve:
        log.info("ROUTING_CACHE_SERVE band=%s sim=%.4f -- classifier skipped",
                 decision.band, decision.similarity or 0.0)
        return classification_from(decision), routing_td(decision)

    # S-2a (2026-09-30): INFO, not DEBUG. The deployment runs LOG_LEVEL=INFO, so this line
    # never appeared, and with it the answer to "why did the cache decline?" -- the only
    # thing that can size the S-2 population: a GENUINE hit refused solely because its band
    # sits outside the serve allowlist (reason="band_not_served"). The same blind spot on
    # the NPU path hid a 30.7% timeout rate behind a logger.debug until 2026-09-30.
    #
    # Band and similarity are logged too, because the reason alone cannot separate a refused
    # HIT from a lookup MISS: routing_cache.serve() populates band/similarity on BOTH, so a
    # "below_threshold" decline also carries a nearest-neighbor band that was never a hit.
    # Reading the reason without the band would overcount the S-2 population.
    #
    # Volume is bounded: declines run about 1,690 per 14 days (5,177 decisions less 3,487
    # served), roughly 120 a day, so this cannot flood the log.
    log.info("ROUTING_CACHE_DECLINE reason=%s band=%s sim=%.4f -- classifying",
             decision.reason, decision.band, decision.similarity or 0.0)
    return await classify_fn(prompt, file_count, hint)
