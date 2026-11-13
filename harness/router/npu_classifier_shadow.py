"""
NPU classifier shadow-mode (v96).
Calls FLM :8003 with the standalone-tested classifier prompt.
Returns structured JSON or None on any failure. Never raises.

Phase 1 standalone eval (2026-05-01):
  Accuracy 60% (12/20 spanning TRIVIAL/MODERATE/COMPLEX/FRONTIER)
  Parse failures 0/20
  Avg latency 2.27s
  Requires /no_think directive in system prompt -- without it,
  qwen3:1.7b emits early-stop tokens mid-JSON (Q4 quant artifact).
"""
import json
import os
import time
import httpx

from npu_gate import npu_serialized
from config import NPU_TIMEOUT_S
import logging

logger = logging.getLogger("bifrost.npu_classifier")

NPU_CLASSIFIER_ENABLED = os.getenv("NPU_CLASSIFIER_ENABLED", "true").lower() == "true"
NPU_CLASSIFIER_TIMEOUT = float(os.getenv("NPU_CLASSIFIER_TIMEOUT", "60.0"))

# --- R-18 GUARD (2026-09-07) ------------------------------------------------------
# classifier.py wraps npu_classify_ex in asyncio.shield(), so routing gives up at
# NPU_TIMEOUT_S but the in-flight FLM request is NOT canceled. That makes THIS httpx
# timeout the real upper bound on the request's lifetime -- and an httpx timeout firing
# mid-prefill is itself a disconnect, i.e. the v0.9.45 double-free one step later.
# So it MUST exceed NPU_TIMEOUT_S. The previous default (5.0) did not, and the
# invariant was held only by an env var; unset it and R-18 silently returns.
# Clamp rather than refuse to boot: NPU routing is an optimization with a rule-engine
# fallback, so an outage would be a worse trade than a slow classifier.
_NPU_T_FLOOR = float(NPU_TIMEOUT_S) * 2.0
if NPU_CLASSIFIER_TIMEOUT <= float(NPU_TIMEOUT_S):
    logger.error(
        "NPU_CLASSIFIER_TIMEOUT=%.1fs is <= NPU_TIMEOUT_S=%.1fs. An httpx timeout "
        "firing mid-prefill disconnects FLM and re-arms the R-18 double-free. "
        "Clamping to %.1fs. Set NPU_CLASSIFIER_TIMEOUT above NPU_TIMEOUT_S explicitly.",
        NPU_CLASSIFIER_TIMEOUT, float(NPU_TIMEOUT_S), _NPU_T_FLOOR,
    )
    NPU_CLASSIFIER_TIMEOUT = _NPU_T_FLOOR
# ----------------------------------------------------------------------------------
# 3d-1 (2026-09-30): the GATE wait and the REQUEST timeout are different budgets, and they were
# sharing one value. Routing abandons the classification at NPU_TIMEOUT_S, while a successful FLM
# call needs p95 8.0 s (measured 2026-09-30, warm, n=13; p50 6.3 s over 999 real calls). So a
# request that waits longer than the difference CANNOT finish inside the deadline: it pays the
# full stall and then falls back to the rule classifier anyway, which is the same outcome it would
# have reached instantly. Measured cost of not bounding it: when another attempt arrived within
# 7 s, 80.1% of calls timed out (141 of 176 over 14 days).
#
# Bound the WAIT only. The httpx timeout above must stay ABOVE NPU_TIMEOUT_S for R-18, and
# NpuBusy is raised BEFORE anything is sent, so this can never disconnect FLM mid-prefill.
_NPU_P95_CALL_S = 8.0
_GATE_WAIT_S = max(0.5, float(NPU_TIMEOUT_S) - _NPU_P95_CALL_S)
# Logged because the swing is behaviourally large and driven by an env DEFAULT: with
# NPU_TIMEOUT_S=12 (ConfigMap) this is 4.0 s, but on the code default of 8 it collapses to the
# 0.5 s floor -- correct (an 8 s call inside an 8 s deadline affords no wait at all) and easy to
# ship unnoticed. One INFO line at import makes the live budget readable instead of inferred.
logger.info(
    "npu gate budgets: wait<=%.1fs, request<=%.1fs, routing deadline %.1fs (p95 call %.1fs)",
    _GATE_WAIT_S, NPU_CLASSIFIER_TIMEOUT, float(NPU_TIMEOUT_S), _NPU_P95_CALL_S,
)
FLM_URL = os.getenv("FLM_URL", "http://192.168.x.50:8003/v1/chat/completions")
FLM_MODEL = os.getenv("FLM_CLASSIFIER_MODEL", "gemma4-it:e4b")

CLASSIFIER_SYSTEM = """/no_think
You are a routing classifier for a code-and-research assistant.
Read the user prompt and output exactly ONE JSON object, no preamble, no markdown,
no explanation outside the JSON.

Schema:
  {"band": "TRIVIAL"|"MODERATE"|"COMPLEX"|"FRONTIER", "confidence": <float 0.0-1.0>, "reason": "<one sentence>"}

Band definitions:
  TRIVIAL: factual lookup, single-line code, definition, simple completion.
  MODERATE: small refactor, single-file edit, focused analysis.
  COMPLEX: multi-step reasoning, architecture decisions, multi-file refactor.
  FRONTIER: novel research, deep cross-domain synthesis, expert-level original work.

Output the JSON object and nothing else."""


async def npu_classify_async(prompt: str, timeout: float = NPU_CLASSIFIER_TIMEOUT) -> dict | None:
    body = {
        "model": FLM_MODEL,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 200,
        "temperature": 0.1,
        "stream": False,
    }
    t0 = time.time()
    try:
        # R-18: same gate as npu_classifier.py. THIS is the module main.py actually
        # imports (main.py:214 -> npu_classify_async), so leaving it ungated in v338 left
        # the hot path wide open -- four concurrent queued requests were observed.
        # Bounded by this call's own timeout; NpuBusy is a TimeoutError, so the existing
        # except-block below returns None and routing falls back exactly as designed.
        async with npu_serialized(FLM_URL, wait_timeout=_GATE_WAIT_S):
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(FLM_URL, json=body)
                r.raise_for_status()
                data = r.json()
        elapsed_ms = int((time.time() - t0) * 1000)
        raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
        first = raw.find("{")
        last = raw.rfind("}")
        if first < 0 or last <= first:
            return None
        parsed = json.loads(raw[first:last+1])
        parsed["latency_ms"] = elapsed_ms
        return parsed
    except Exception as e:
        # WARNING, not DEBUG (3c, 2026-09-30). Mirrors npu_classifier.py: the exception TYPE is
        # the whole diagnosis -- a ConnectError and an NpuBusy are different failures. At DEBUG
        # under LOG_LEVEL=INFO both were invisible, and a 30.7% timeout rate (443 of 1,442
        # attempts over 14 days) had to be reconstructed from SQLite because of this one line.
        logger.warning("npu_classify_async failed (%s): %s", type(e).__name__, e)
        return None
