"""Anthropic-native passthrough lane -- Forge/Igor ONLY.

Implements PACKET-router-anthropic-passthrough-lane-2026-08-26 (signed by John 2026-08-26).

WHY THIS IS A SEPARATE MODULE. `main.py` is on the protected list. Everything that can live
outside it does, so the protected-file diff is a hook, a route, and a catalog entry rather than
several hundred lines.

WHAT THIS LANE IS NOT. It is NOT the /v1/messages shim. The shim translates
Anthropic -> OpenAI -> tier cascade -> Anthropic, and that is still the default for every other
model id. This lane forwards the Anthropic body to Ollama's own /v1/messages on Forge, because
Ollama serves that format natively (verified 2026-08-26: 6 of 6 lane acceptance tests, including
a tool_use round trip the shim cannot do).

THE THREE SCOPE LIMITS ARE STRUCTURAL, NOT ADVISORY (packet section 4):

  1. FORGE/IGOR ONLY, NO CASCADE. This module never consults get_cascade() and has no fallback
     path. If Igor is unavailable the caller gets an error. Bifrost cannot serve 64K -- a 16 GB
     card with ~4.5 GB on two 4K displays and granite pinned at 6.7 GB.
  2. CONCURRENCY CAP OF 1. Ollama clamps NUM_PARALLEL to 1 for a model this size anyway; the
     explicit cap makes queueing visible instead of letting a burst stack. This is not
     theoretical -- block 47 (2026-08-26) measured that 2 concurrent small requests DEGRADE a
     large-context request's output into harmony-control-token soup rather than merely slowing
     it: 9 failures in 9 tasks, against 0 in 10 quiet ones.
  3. NO RAG ENRICHMENT. Enrichment mutates the body, which fights the forwarding contract. The
     hook in main.py returns before the shim's enrichment block for exactly this reason.
"""
import asyncio
import json
import logging
import os

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config import TIER_BACKENDS, Tier

log = logging.getLogger(__name__)

# The id Claude Code will actually keep. Its gateway discovery drops every id that does not
# contain "claude" or "anthropic", which is why the fleet's own "bifrost-*" ids are invisible to
# it (packet 3.3, verified by probe).
PASSTHROUGH_MODEL_ID = "bifrost/claude-igor"

# Config, not literals buried in a handler (packet 2, requirement 2).
MAX_TOKENS_FLOOR = int(os.environ.get("BIFROST_PT_MAX_TOKENS_FLOOR", "4096"))
CONCURRENCY = int(os.environ.get("BIFROST_PT_CONCURRENCY", "1"))
PING_INTERVAL_S = float(os.environ.get("BIFROST_PT_PING_S", "20"))
TIMEOUT_S = float(os.environ.get("BIFROST_PT_TIMEOUT_S", "900"))

_SEM = asyncio.Semaphore(CONCURRENCY)

# Forwarded verbatim as OPEN LISTS. Do NOT allowlist observed values -- Claude Code gains
# capabilities each release, and an allowlist silently disables the new ones (packet 3.3).
_FORWARD_HEADERS = ("anthropic-version", "anthropic-beta")

_PING = b'event: ping\ndata: {"type": "ping"}\n\n'


def is_passthrough_model(model) -> bool:
    return bool(model) and str(model).strip() == PASSTHROUGH_MODEL_ID


def upstream():
    """Resolve Forge/Igor from TIER_BACKENDS, never a hardcoded IP.

    TIER_BACKENDS is the live read-through proxy over fleet_config.json, so a tier repoint is
    picked up here on the same mtime cache as everywhere else.
    """
    b = TIER_BACKENDS[Tier.T2_5_SOLO]
    return b["base_url"].rstrip("/"), b["model"]


def _fwd_headers(request) -> dict:
    h = {"content-type": "application/json"}
    for k in _FORWARD_HEADERS:
        v = request.headers.get(k)
        if v:
            h[k] = v
    return h


def rewrite_max_tokens(body: dict):
    """Rewrite max_tokens UPWARD to the floor. Returns (original, effective).

    Anthropic Messages makes max_tokens MANDATORY, so it cannot be stripped the way the OpenAI
    lane strips it -- it must be REWRITTEN. Igor is a thinking model and max_tokens caps thinking
    and text TOGETHER, so a small client value yields HTTP 200, a well-formed response,
    stop_reason "max_tokens", and ZERO text. Measured on Igor: 16/32/64 all produced empty text;
    128 was the floor for a trivial arithmetic prompt and carries no margin for a real turn with
    a system prompt and tools.

    The client's original value is returned so it can be kept as a METRIC. It is never enforced
    on a local model.
    """
    original = body.get("max_tokens")
    try:
        orig_i = int(original)
    except (TypeError, ValueError):
        orig_i = 0
    effective = max(orig_i, MAX_TOKENS_FLOOR)
    body["max_tokens"] = effective
    return orig_i, effective


def _text_len(resp: dict) -> int:
    n = 0
    for blk in resp.get("content") or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            n += len(blk.get("text") or "")
    return n


def estimate_tokens(body: dict) -> int:
    """chars/4 estimate over every text-bearing field, matching the router's existing convention.

    Deliberately an ESTIMATE. The point (packet 3.1) is to stop Claude Code counting context
    THROUGH the inference endpoint -- every context check today is a real inference call against
    a 64.5 GB model, which is the single largest multiplier on lane traffic. An estimate removes
    the multiplier; exactness is not what is being bought.
    """
    chars = 0

    def walk(x):
        nonlocal chars
        if isinstance(x, str):
            chars += len(x)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("type", "role"):
                    continue
                walk(v)

    walk(body.get("system"))
    walk(body.get("messages"))
    walk(body.get("tools"))
    return max(1, chars // 4)


async def _post_json(url, headers, payload):
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
        r = await c.post(url, headers=headers, content=json.dumps(payload).encode())
        return r.status_code, r.content, r.headers.get("content-type", "application/json")


async def handle(request, body: dict, stream: bool):
    """Entry point. Returns a Response; never raises into the caller's cascade."""
    base, model = upstream()
    url = base + "/v1/messages"

    orig, eff = rewrite_max_tokens(body)
    body["model"] = model
    headers = _fwd_headers(request)

    if orig != eff:
        log.info(
            "PASSTHROUGH max_tokens rewritten %s -> %s (floor=%s) model=%s stream=%s",
            orig, eff, MAX_TOKENS_FLOOR, model, stream,
        )

    if stream:
        return await _stream(url, headers, body, orig, eff, model)
    return await _unary(url, headers, body, orig, eff, model)


async def _unary(url, headers, body, orig, eff, model):
    async with _SEM:
        try:
            status, raw, ctype = await _post_json(url, headers, body)
        except Exception as e:
            # NO CASCADE. Scope limit 1: this lane fails rather than falling through.
            log.error("PASSTHROUGH upstream unreachable: %s: %s", type(e).__name__, e)
            return JSONResponse(
                {"type": "error",
                 "error": {"type": "api_error",
                           "message": "passthrough upstream unreachable: %s: %s"
                                      % (type(e).__name__, e)}},
                status_code=502,
            )

    if status >= 400:
        # VERBATIM RELAY (packet 3.3). Claude Code's retry logic matches on the upstream's own
        # error wording and disables the rejected capability for the rest of the conversation;
        # re-wrapping the body breaks that recovery even with the status code preserved.
        log.warning("PASSTHROUGH upstream %s relayed verbatim (%d bytes)", status, len(raw))
        return Response(content=raw, status_code=status, media_type=ctype)

    try:
        resp = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return Response(content=raw, status_code=status, media_type=ctype)

    # THE DEFECT CANARY (packet 2, requirement 4). stop_reason "max_tokens" with no text is a
    # lane defect, not an answer, and under byte-for-byte passthrough it is reachable from the
    # client as a silent well-formed HTTP 200. With the floor applied, hitting it means the model
    # burned the whole rewritten budget on thinking -- worth failing loudly rather than relaying
    # an empty success. Both caps are logged so it is diagnosable from the log alone.
    if resp.get("stop_reason") == "max_tokens" and _text_len(resp) == 0:
        log.error(
            "PASSTHROUGH LANE DEFECT: stop_reason=max_tokens with ZERO text. "
            "client max_tokens=%s effective=%s floor=%s model=%s usage=%s",
            orig, eff, MAX_TOKENS_FLOOR, model, resp.get("usage"),
        )
        return JSONResponse(
            {"type": "error",
             "error": {"type": "api_error",
                       "message": ("passthrough lane defect: model returned stop_reason=max_tokens "
                                   "with no text (client max_tokens=%s, effective=%s). "
                                   "This is a lane fault, not a model answer." % (orig, eff))}},
            status_code=502,
        )
    return Response(content=raw, status_code=status, media_type=ctype)


async def _stream(url, headers, body, orig, eff, model):
    """Stream with our OWN ping emission.

    Claude Code counts every byte and aborts a stream silent for 300s. A cold Igor load is
    64.5 GB. Pings are emitted during ANY silence, which covers cold load, queueing behind the
    concurrency cap, and long prefill alike.

    The upstream status is checked BEFORE a StreamingResponse is constructed -- once streaming
    starts the status is already on the wire and a verbatim error relay is impossible.
    """
    client = httpx.AsyncClient(timeout=TIMEOUT_S)
    await _SEM.acquire()
    try:
        ctx = client.stream("POST", url, headers=headers, content=json.dumps(body).encode())
        r = await ctx.__aenter__()
    except Exception as e:
        _SEM.release()
        await client.aclose()
        log.error("PASSTHROUGH stream upstream unreachable: %s: %s", type(e).__name__, e)
        return JSONResponse(
            {"type": "error",
             "error": {"type": "api_error",
                       "message": "passthrough upstream unreachable: %s: %s"
                                  % (type(e).__name__, e)}},
            status_code=502,
        )

    if r.status_code >= 400:
        raw = await r.aread()
        ctype = r.headers.get("content-type", "application/json")
        await ctx.__aexit__(None, None, None)
        _SEM.release()
        await client.aclose()
        log.warning("PASSTHROUGH stream upstream %s relayed verbatim (%d bytes)",
                    r.status_code, len(raw))
        return Response(content=raw, status_code=r.status_code, media_type=ctype)

    async def gen():
        q = asyncio.Queue()

        async def pump():
            try:
                async for chunk in r.aiter_raw():
                    await q.put(("d", chunk))
                await q.put(("end", None))
            except Exception as ex:
                await q.put(("exc", ex))

        task = asyncio.create_task(pump())
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=PING_INTERVAL_S)
                except asyncio.TimeoutError:
                    yield _PING
                    continue
                if kind == "d":
                    yield payload
                elif kind == "end":
                    break
                else:
                    log.error("PASSTHROUGH mid-stream error: %s", payload)
                    yield (b"event: error\ndata: " + json.dumps(
                        {"type": "error",
                         "error": {"type": "api_error", "message": str(payload)}}
                    ).encode() + b"\n\n")
                    break
        finally:
            task.cancel()
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
            _SEM.release()
            await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")
