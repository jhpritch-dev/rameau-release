"""
BIFROST Complexity Router -- Rule-Based Classifier
===================================================
Phase 1 heuristic classifier. Scores incoming requests into
TRIVIAL / MODERATE / COMPLEX / FRONTIER bands.

Phase 2+ replaces this with a learned classifier on Forge NPU.
The rule-based version remains as the WORKSHOP-mode fallback
(Forge offline -> no NPU -> heuristic).
"""

import asyncio as _asyncio
import logging as _logging
import re
import time as _time
from dataclasses import dataclass

from config import (
    ComplexityBand,
    classifier_config,
    NPU_CONFIDENCE_THRESHOLD,
    NPU_ROUTING_ENABLED,
    NPU_TIMEOUT_S,
)

_log = _logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword matching helper (2026-06-25 npu-classifier-embed brief)
# ---------------------------------------------------------------------------
# Substring-misroute fix: the keyword loops in classify() previously used
# `kw in text`, which false-matched INTERNAL substrings -- "api" in "c-api-tal",
# "test" in "la-test", "type" in "proto-type", "sort" in "re-sort". A naive
# `\bkw\b` word-boundary would break the INTENTIONAL prefix stems in the config
# lists ("serializ" -> serialization/serialize, "deserializ" -> ...).
# Left-word-boundary matching (`\bkw`) kills the internal-substring false
# positives while preserving prefix stems: r"\bapi" does NOT match "capital",
# but r"\bserializ" still matches "serialization". Multi-word phrase keywords
# ("what is", "rate limit") keep working (\b binds the leading token).
# Patterns are compiled once per keyword and cached.
_KW_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _kw_hit(kw: str, text: str) -> bool:
    """True if `kw` occurs in `text` at a left word boundary (see note above)."""
    pat = _KW_PATTERN_CACHE.get(kw)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(kw))
        _KW_PATTERN_CACHE[kw] = pat
    return pat.search(text) is not None


@dataclass
class ClassificationResult:
    band: ComplexityBand
    confidence: float           # 0.0-1.0
    reasoning: str              # Human-readable explanation
    scores: dict[str, float]    # Per-band scores for debugging
    hint_override: bool = False # True if X-Complexity-Hint was used


# ---------------------------------------------------------------------------
# Web search intent detection (v104)
# ---------------------------------------------------------------------------

_WEB_SEARCH_TEMPORAL = re.compile(
    r'\b(latest|current|today|tonight|this (week|month|year)|yesterday|'
    r'right now|as of|breaking|news|headlines|top stor(y|ies)|score|weather|price|stock|'
    r'who (is|are|won|leads)|what (is the|are the) current|'
    r'recently|just announced|released today|on this date|trending|standings)\b',
    re.IGNORECASE,
)

_WEB_SEARCH_EXPLICIT = re.compile(
    r'\b(search (for|the web|online|the internet)|web search|'
    r'google (it|for)|browse the (web|internet)|'
    r'look (it )?up( online)?|look up|find out|fetch|'
    r'check (online|the web)|'
    r'what does .{0,30} say today|check .{0,30} website)\b',
    re.IGNORECASE,
)


def detect_web_search_intent(prompt: str) -> bool:
    """Returns True if the prompt likely requires live web data."""
    return bool(
        _WEB_SEARCH_TEMPORAL.search(prompt) or
        _WEB_SEARCH_EXPLICIT.search(prompt)
    )


def detect_explicit_web_search(prompt: str) -> bool:
    """True only when the caller ASKED for a live lookup, as opposed to merely using
    time-sensitive words. Kept separate from detect_web_search_intent (which stays
    byte-identical, so every existing caller is unaffected) so that an explicit request can
    bypass the confidence gate while keyword intent stays subject to it -- see main.py
    _fire_web. Calibrated 2026-09-29 both directions: 0/9 misses on explicit asks (the old
    pattern missed 6/9) and 0/10 false positives on prompts that merely mention search,
    including "binary search", "search endpoint" and "Google's PageRank"."""
    return bool(_WEB_SEARCH_EXPLICIT.search(prompt))


# ---------------------------------------------------------------------------
# KB intent detection (v106) -- bypasses TRIVIAL/MODERATE band gate for RAG
# ---------------------------------------------------------------------------

_KB_DOC_REFERENCE = re.compile(
    r'\b(based on (our|the|your)|according to (our|the)|'
    r'in (our|the) (document|knowledge|kb|database|corpus|repo|codebase)|'
    r'from (our|the) (knowledge base|documents|files|inhouse|pk)|'
    r'using (our|the) (docs|knowledge|context)|'
    r'what does (our|the).{0,30}say|'
    r'summarize (our|the|this))\b',
    re.IGNORECASE,
)


def detect_kb_intent(prompt: str, project_header: str = "") -> bool:
    """
    Returns True if the query should trigger KB enrichment regardless of band.
    Fires when:
      - X-Bifrost-Project header is set to a non-default namespace, OR
      - Prompt contains explicit document-reference language.
    """
    if project_header and project_header not in ("default", ""):
        return True
    return bool(_KB_DOC_REFERENCE.search(prompt))


def classify(
    prompt: str,
    file_count: int = 0,
    hint: str | None = None,
) -> ClassificationResult:
    """
    Classify a request into a complexity band.

    Args:
        prompt: The user's message / code prompt.
        file_count: Number of files referenced (from request metadata).
        hint: Optional client hint (X-Complexity-Hint header).

    Returns:
        ClassificationResult with band, confidence, and reasoning.
    """
    cfg = classifier_config

    # ------------------------------------------------------------------
    # 1. Client hint override (trust the caller if they say so)
    # ------------------------------------------------------------------
    if hint and hint.lower() in ("trivial", "moderate", "complex", "frontier"):
        band = ComplexityBand(hint.upper())
        return ClassificationResult(
            band=band,
            confidence=0.9,
            reasoning=f"Client hint override: X-Complexity-Hint={hint}",
            scores={},
            hint_override=True,
        )

    # ------------------------------------------------------------------
    # 2. Feature extraction
    # ------------------------------------------------------------------
    prompt_lower = prompt.lower()
    token_estimate = len(prompt.split())  # Rough whitespace tokenizer
    reasons = []

    # ------------------------------------------------------------------
    # 3. Keyword scoring
    # ------------------------------------------------------------------
    scores = {
        "TRIVIAL": 0.0,
        "MODERATE": 0.0,
        "COMPLEX": 0.0,
        "FRONTIER": 0.0,
    }

    for kw in cfg.trivial_keywords:
        if _kw_hit(kw, prompt_lower):
            scores["TRIVIAL"] += 1.0

    for kw in cfg.moderate_keywords:
        if _kw_hit(kw, prompt_lower):
            scores["MODERATE"] += 1.5  # Strong signal -- these words mean real work

    for kw in cfg.complex_keywords:
        if _kw_hit(kw, prompt_lower):
            scores["COMPLEX"] += 2.0  # Weighted higher -- these are strong signals

    for kw in cfg.frontier_keywords:
        if _kw_hit(kw, prompt_lower):
            scores["FRONTIER"] += 2.5  # Strongest signal

    # ------------------------------------------------------------------
    # 4. Token count signal
    # ------------------------------------------------------------------
    if token_estimate <= cfg.trivial_max_tokens:
        scores["TRIVIAL"] += 1.5
        reasons.append(f"short prompt ({token_estimate} tokens)")
    elif token_estimate <= cfg.moderate_max_tokens:
        scores["MODERATE"] += 1.5
        reasons.append(f"medium prompt ({token_estimate} tokens)")
    elif token_estimate <= cfg.complex_max_tokens:
        scores["COMPLEX"] += 1.5
        reasons.append(f"long prompt ({token_estimate} tokens)")
    else:
        scores["FRONTIER"] += 2.0
        reasons.append(f"very long prompt ({token_estimate} tokens)")

    # ------------------------------------------------------------------
    # 5. File count signal
    # ------------------------------------------------------------------
    if file_count >= cfg.multi_file_threshold:
        scores["COMPLEX"] += 2.0
        scores["FRONTIER"] += 1.0
        reasons.append(f"multi-file ({file_count} files)")
    elif file_count > 1:
        scores["MODERATE"] += 1.0
        reasons.append(f"{file_count} files referenced")

    # ------------------------------------------------------------------
    # 6. Structural signals
    # ------------------------------------------------------------------

    # Code blocks in prompt suggest providing context -> higher complexity
    code_blocks = len(re.findall(r"```", prompt))
    if code_blocks >= 4:
        scores["COMPLEX"] += 1.5
        reasons.append(f"{code_blocks // 2}+ code blocks")
    elif code_blocks >= 2:
        scores["MODERATE"] += 1.0

    # Question marks -- simple questions tend to be lower complexity
    questions = prompt.count("?")
    if questions == 0 and token_estimate < 50:
        # Very short, no question -- likely autocomplete/fill
        # BUT only if no higher-band keywords triggered
        has_higher_keywords = (
            scores["MODERATE"] > 0 or scores["COMPLEX"] > 0 or scores["FRONTIER"] > 0
        )
        if not has_higher_keywords:
            scores["TRIVIAL"] += 1.5
            reasons.append("no question, very short, no keywords -> likely autocomplete")

    # Multi-step indicators
    step_patterns = [
        r"(?:first|then|next|after that|finally|step \d)",
        r"(?:1\.|2\.|3\.)",
        r"(?:phase|stage|part \d)",
    ]
    for pattern in step_patterns:
        if re.search(pattern, prompt_lower):
            scores["COMPLEX"] += 1.0
            reasons.append("multi-step language detected")
            break

    # ------------------------------------------------------------------
    # 7. Determine band from scores
    # ------------------------------------------------------------------
    # On ties, prefer the HIGHER complexity band when keyword evidence
    # supports it. Rationale: under-routing costs quality (bad output),
    # over-routing costs only latency (still correct output). Safety bias.
    band_order = ["TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"]
    max_score = max(scores.values())

    if max_score == 0:
        # No signals at all -- default to MODERATE (safe middle ground)
        band = ComplexityBand.MODERATE
        confidence = 0.3
        reasons.append("no strong signals, defaulting to MODERATE")
    else:
        # Among bands with the max score, pick the HIGHEST complexity one
        # This is the safety bias: better to over-route than under-route
        for b in reversed(band_order):
            if scores[b] == max_score:
                band = ComplexityBand(b)
                break

        # Confidence based on margin between winner and runner-up
        sorted_scores = sorted(scores.values(), reverse=True)
        margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
        confidence = min(0.95, 0.5 + (margin * 0.1))

    return ClassificationResult(
        band=band,
        confidence=confidence,
        reasoning="; ".join(reasons) if reasons else "keyword scoring",
        scores=scores,
    )

# ---------------------------------------------------------------------------
# v98 -- confidence-gated NPU routing wrapper
# ---------------------------------------------------------------------------
# Calls rule-based classify() always (cheap, sync, deterministic) and may also
# call NPU classifier asynchronously. Decision based on NPU_ROUTING_ENABLED:
#   off     -- rule decides; NPU not called
#   shadow  -- NPU called for telemetry; rule decides
#   live    -- NPU decides when confidence >= NPU_CONFIDENCE_THRESHOLD;
#              rule fallback on low-conf, timeout, or NPU error.
# Returns (final_result, telemetry_dict) -- telemetry surfaces routing-source,
# NPU prediction/confidence/latency, rule prediction, fallback reason, agreement.
# ---------------------------------------------------------------------------

# (Module-level imports and _log relocated to top of file)

# R-18: refs for NPU classifications we have stopped waiting for. A bare task can be
# garbage collected mid-flight (the v97 lesson already recorded in main.py), and collecting
# it would close the socket -- the very disconnect this change exists to avoid. Held until
# the request completes on its own.
_npu_abandoned: set = set()


def _drain_npu_task(task) -> None:
    """Consume an abandoned classification's outcome so asyncio does not log
    'Task exception was never retrieved'. The value is deliberately discarded: whoever
    stopped waiting has already fallen back to rule-based routing."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.debug("[router] abandoned NPU classification ended in %r", exc)


async def classify_with_npu(
    prompt: str,
    file_count: int = 0,
    hint: str | None = None,
) -> tuple[ClassificationResult, dict]:
    """v98 confidence-gated routing wrapper. See module docstring above."""
    rule_result = classify(prompt, file_count, hint)

    if NPU_ROUTING_ENABLED == "off":
        return rule_result, {
            "routing_source": "rule",
            "rule_pred": rule_result.band.value,
            "npu_pred": None,
            "npu_conf": None,
            "npu_latency_ms": None,
            "fallback_reason": None,
            "agreed": None,
        }

    # 2026-10-13: a valid X-Complexity-Hint is the caller's explicit band -- classify() honors it
    # first ("trust the caller if they say so"). The live NPU-wins branch below replaced that band
    # while keeping hint_override=True, so the hint was discarded and the flag claimed otherwise.
    # Live mode now returns here without asking the NPU. Shadow is unchanged: the NPU still runs
    # for telemetry and the rule result (the hint) decides, as it always did.
    if rule_result.hint_override and NPU_ROUTING_ENABLED != "shadow":
        return rule_result, {
            "routing_source": "hint",
            "rule_pred": rule_result.band.value,
            "npu_pred": None,
            "npu_conf": None,
            "npu_latency_ms": None,
            "fallback_reason": None,
            "agreed": None,
        }

    npu_pred = None
    npu_conf = None
    npu_latency = None
    npu_error = None

    try:
        from npu_classifier import npu_classify_ex
        t0 = _time.perf_counter()
        # R-18 (root-caused 2026-08-13): do NOT let this timeout cancel the NPU call.
        # asyncio.wait_for CANCELS its coroutine on expiry, tearing down the HTTP
        # connection while FLM is mid-prefill -- the exact trigger for the v0.9.45
        # double-free that killed flm.service six times in one day. The Router was the
        # client that disconnected.
        #
        # shield() separates "stop waiting" from "kill the request": routing still gives
        # up at NPU_TIMEOUT_S and falls back to rules exactly as before, but the in-flight
        # request is allowed to finish and its answer is discarded. npu_gate serializes the
        # NPU, so an abandoned call delays only the next NPU call. npu_classify's own httpx
        # timeout (NPU_CLASSIFIER_TIMEOUT) is now the real upper bound on its lifetime, and
        # must therefore be set LONGER than NPU_TIMEOUT_S, not shorter -- an httpx timeout
        # firing mid-prefill is itself a disconnect, i.e. the same crash one step later.
        _npu_task = _asyncio.ensure_future(npu_classify_ex(prompt))
        _npu_abandoned.add(_npu_task)
        _npu_task.add_done_callback(_npu_abandoned.discard)
        _npu_task.add_done_callback(_drain_npu_task)
        npu_outcome = await _asyncio.wait_for(
            _asyncio.shield(_npu_task),
            timeout=NPU_TIMEOUT_S,
        )
        npu_latency = (_time.perf_counter() - t0) * 1000.0
        npu_result, npu_detail = npu_outcome
        if npu_result is not None:
            npu_pred = npu_result.prediction.value
            npu_conf = float(npu_result.confidence)
        else:
            # PACKET-70: keep the "npu_error" PREFIX. Prometheus
            # (bifrost_routing_fallback_total, labeled on this string) and every existing
            # query match on it; a bare rename would silently zero them. Detail is appended,
            # so `LIKE 'npu_error%'` keeps working and the cause is finally durable.
            npu_error = "npu_error:%s" % (npu_detail or "unknown")
    except _asyncio.TimeoutError:
        npu_error = "npu_timeout"
        _log.warning("[router] NPU classifier timed out after %ss, using rule fallback", NPU_TIMEOUT_S)
    except Exception as e:
        npu_error = "npu_error:outer_exc:%s" % type(e).__name__
        _log.warning("[router] NPU classifier error (%s): %s, using rule fallback",
                     type(e).__name__, e)

    agreed = (npu_pred == rule_result.band.value) if npu_pred else None

    if NPU_ROUTING_ENABLED == "shadow":
        # Shadow: NPU runs (telemetry only), rule decides.
        return rule_result, {
            "routing_source": "rule",
            "rule_pred": rule_result.band.value,
            "npu_pred": npu_pred,
            "npu_conf": npu_conf,
            "npu_latency_ms": npu_latency,
            "fallback_reason": npu_error,
            "agreed": agreed,
        }

    # Live mode below.
    if npu_error:
        return rule_result, {
            "routing_source": "fallback",
            "rule_pred": rule_result.band.value,
            "npu_pred": None,
            "npu_conf": None,
            "npu_latency_ms": npu_latency,
            "fallback_reason": npu_error,
            "agreed": None,
        }

    if npu_conf is None or npu_conf < NPU_CONFIDENCE_THRESHOLD:
        return rule_result, {
            "routing_source": "fallback",
            "rule_pred": rule_result.band.value,
            "npu_pred": npu_pred,
            "npu_conf": npu_conf,
            "npu_latency_ms": npu_latency,
            "fallback_reason": "low_confidence",
            "agreed": agreed,
        }

    # NPU wins.
    # FLEET-HYGIENE-SWEEP Item C (2026-05-23): preserve rule_result.hint_override
    # so the NPU-wins branch doesn't drop the X-Complexity-Hint signal. Prior
    # behavior defaulted hint_override=False here, making the flag unreliable on
    # high-confidence NPU paths (see v120 SHADOW_GUARD workaround at
    # main.py:1407 which checks both the flag AND the raw hint arg to compensate).
    final = ClassificationResult(
        band=ComplexityBand(npu_pred),
        confidence=npu_conf,
        reasoning=f"NPU classifier confidence={npu_conf:.2f}",
        scores={},
        hint_override=rule_result.hint_override,
    )
    return final, {
        "routing_source": "npu",
        "rule_pred": rule_result.band.value,
        "npu_pred": npu_pred,
        "npu_conf": npu_conf,
        "npu_latency_ms": npu_latency,
        "fallback_reason": None,
        "agreed": agreed,
    }

