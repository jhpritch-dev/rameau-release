"""
BIFROST Router -- context-overflow pre-flight tripwire.
========================================================
Estimates the incoming prompt token count and compares it to the chosen
tier's context window (num_ctx from fleet_config.json). Makes context
overflow LOUD instead of letting Ollama silently front-truncate the prompt
-- silent truncation drops the brief and completion criteria out of the
window, producing sessions that never self-terminate and confabulated
completion reports.

Same philosophy as the classifier/embed dependency hardening: never degrade
silently. This is a tripwire, not a meter -- a crude chars/4 estimate is
enough to catch a 200k-token session aimed at a 131k window.

Modes (env BIFROST_CTX_OVERFLOW, default "reject"; read at call time so a
kubectl set env rollback takes effect on the next pod without a code change):
  reject -- local backend over 100% of ctx_len -> HTTP 413; cloud -> warn-only.
  warn   -- never 413; over-budget logs a WARN (local AND cloud).
  off    -- checks disabled entirely.

Pressure band (PRESSURE_RATIO <= ratio <= 1.0): emit x-bifrost-ctx-pressure
header + a WARN (per-alias 5-minute cooldown); the request proceeds.

Cloud backends are warn-only by design -- the provider enforces its own
context limit loudly (HTTP 400) already, so a local 413 would be redundant.
"""
import json
import os
import time

PRESSURE_RATIO = 0.85
WARN_COOLDOWN_S = 300.0  # 5 minutes, matches the classifier/embed shadow cooldown
LOCAL_BACKEND_TYPES = ("ollama", "llama_server")
PRESSURE_HEADER = "x-bifrost-ctx-pressure"

# Per-alias last-WARN timestamp. Module-level (never per-request) so the
# cooldown dedups across requests, exactly like _classifier_warn_ts in main.
_warn_ts: "dict[str, float]" = {}


def get_mode() -> str:
    """Read BIFROST_CTX_OVERFLOW at call time. Default 'reject'."""
    return (os.environ.get("BIFROST_CTX_OVERFLOW", "reject") or "reject").strip().lower()


def estimate_prompt_tokens(messages, tools=None) -> int:
    """Crude prompt-token estimate: total message chars / 4 + per-message
    overhead + tools-block chars / 4. Handles plain-string content and
    Anthropic list-of-blocks content (text blocks counted directly;
    tool_use/tool_result and other dict blocks counted by their JSON size so
    the estimate over-counts rather than under-counts -- safer for a tripwire.
    """
    total_chars = 0
    n_msg = 0
    for m in (messages or []):
        n_msg += 1
        c = m.get("content", "") if isinstance(m, dict) else m
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    t = b.get("text")
                    if isinstance(t, str):
                        total_chars += len(t)
                    else:
                        try:
                            total_chars += len(json.dumps(b))
                        except Exception:
                            total_chars += len(str(b))
                else:
                    total_chars += len(str(b))
        elif isinstance(c, str):
            total_chars += len(c)
        elif c is not None:
            total_chars += len(str(c))
    tools_chars = 0
    if tools:
        try:
            tools_chars = len(json.dumps(tools))
        except Exception:
            tools_chars = sum(len(str(t)) for t in tools)
    return total_chars // 4 + tools_chars // 4 + n_msg * 4


def is_local_backend(backend_type) -> bool:
    """True for backend types whose context window we own (Ollama/llama-server)."""
    return backend_type in LOCAL_BACKEND_TYPES


def _should_warn(alias: str, now=None) -> bool:
    """True at most once per WARN_COOLDOWN_S per alias; records the emit time."""
    _now = now if now is not None else time.time()
    if _now - _warn_ts.get(alias, 0.0) >= WARN_COOLDOWN_S:
        _warn_ts[alias] = _now
        return True
    return False


def evaluate(est_tokens, ctx_len, *, is_local, alias, mode=None, now=None) -> dict:
    """Core decision. Returns a dict the caller turns into an HTTP response/log:

      action  -- "reject" | "warn" | "pressure" | "ok"
      pct      -- estimated ctx utilization percent (int), or None when disabled
      header   -- (name, value) tuple to add to the response, or None
      body     -- flat 413 JSON body dict when action == "reject", else None
      warn     -- whether the caller should emit msg now (cooldown applied)
      msg      -- the WARN log line, or None

    The cooldown gate is applied here (via _should_warn) so the caller stays a
    thin shim. The 413 is returned every time it applies; only its LOG line is
    throttled.
    """
    mode = (mode or get_mode())
    if mode == "off" or not ctx_len or ctx_len <= 0:
        return {"action": "ok", "pct": None, "header": None, "body": None, "warn": False, "msg": None}

    ratio = est_tokens / ctx_len
    pct = int(round(ratio * 100))
    header = (PRESSURE_HEADER, str(pct))

    if ratio > 1.0:
        if is_local and mode == "reject":
            warn = _should_warn(alias, now)
            return {
                "action": "reject",
                "pct": pct,
                "header": header,
                "body": {
                    "error": "context_overflow",
                    "alias": alias,
                    "ctx_len": ctx_len,
                    "estimated_tokens": est_tokens,
                },
                "warn": warn,
                "msg": (
                    f"CTX_OVERFLOW reject: alias={alias} ctx_len={ctx_len} "
                    f"est={est_tokens} pct={pct} (local, mode=reject)"
                ) if warn else None,
            }
        # cloud over-budget, or mode == "warn": warn-only, request proceeds.
        warn = _should_warn(alias, now)
        return {
            "action": "warn",
            "pct": pct,
            "header": header,
            "body": None,
            "warn": warn,
            "msg": (
                f"CTX_OVERFLOW warn-only: alias={alias} ctx_len={ctx_len} "
                f"est={est_tokens} pct={pct} (local={is_local}, mode={mode})"
            ) if warn else None,
        }

    if ratio >= PRESSURE_RATIO:
        warn = _should_warn(alias, now)
        return {
            "action": "pressure",
            "pct": pct,
            "header": header,
            "body": None,
            "warn": warn,
            "msg": (
                f"CTX_PRESSURE: alias={alias} ctx_len={ctx_len} "
                f"est={est_tokens} pct={pct}"
            ) if warn else None,
        }

    return {"action": "ok", "pct": pct, "header": None, "body": None, "warn": False, "msg": None}


def check_context(messages, tools, backend, alias, default_num_ctx, mode=None, now=None) -> dict:
    """Convenience wrapper: pull ctx_len + backend type from a TIER_BACKENDS
    entry, estimate the prompt, and evaluate. `backend` is the dict from
    config.TIER_BACKENDS[tier] (may be None/empty -> default_num_ctx fallback).
    """
    backend = backend or {}
    ctx_len = backend.get("num_ctx")
    try:
        ctx_len = int(ctx_len) if ctx_len else int(default_num_ctx)
    except (TypeError, ValueError):
        ctx_len = int(default_num_ctx)
    est = estimate_prompt_tokens(messages, tools)
    decision = evaluate(
        est, ctx_len,
        is_local=is_local_backend(backend.get("type")),
        alias=alias,
        mode=mode,
        now=now,
    )
    decision["estimated_tokens"] = est
    decision["ctx_len"] = ctx_len
    return decision
