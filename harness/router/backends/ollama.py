"""
BIFROST Router — Ollama Backend
================================
Proxies OpenAI-compatible requests to local Ollama.
Ollama natively serves /v1/chat/completions, so this is
mostly a pass-through with model override and streaming relay.
"""

import asyncio
import json
import logging
import re
import sys as _sys
import time
import uuid
from typing import AsyncIterator

import httpx

from degenerate import check_openai_response, guard_stream

from npu_gate import npu_uncancellable

from config import Tier, TIER_BACKENDS, settings

log = logging.getLogger(__name__)

# --- PACKET-67 in-process attribution -------------------------------------------------
# Every path to Ollama passes through ollama_chat_completion(). The HTTP handler in main.py
# records its own routing_decisions row; nothing else did, so in-process traffic (autopilot,
# subtask_graph, strategies, router_graph) was invisible to the tables used to attribute
# load. That is how five gemma4 loads happened on 2026-08-19 with zero telemetry rows.

# Tasks are kept referenced until they finish: asyncio only holds a weak reference to a
# bare create_task(), so a fire-and-forget write can be garbage-collected before it runs.
_ATTRIB_TASKS: set = set()


def _inproc_caller(max_depth: int = 12) -> str:
    """Nearest calling frame outside this module, as 'module.function:line'.

    Uses _getframe rather than inspect.stack(): stack() materializes source context for
    every frame, which is far too expensive for a function on the hot path of all local
    inference. Never raises -- an attribution helper must not be able to fail a request.
    """
    try:
        frame = _sys._getframe(1)
        for _ in range(max_depth):
            if frame is None:
                break
            mod = frame.f_globals.get("__name__", "") or ""
            if mod and not mod.endswith("backends.ollama"):
                return "%s.%s:%d" % (mod.rsplit(".", 1)[-1],
                                     frame.f_code.co_name, frame.f_lineno)
            frame = frame.f_back
    except Exception:
        pass
    return "unknown"


def _record_inproc_dispatch(tier, model: str, base_url: str, caller: str) -> None:
    """Log the dispatch, and record a routing_decisions row for NON-HTTP callers.

    Skips the row when the caller is main: that path already writes one, and a second would
    double-count production traffic and quietly invalidate every existing query over this
    table. The log line is emitted either way, so no dispatch goes unobserved.

    Entirely best-effort. Telemetry failing must never fail inference.
    """
    try:
        log.info("ollama dispatch: tier=%s model=%s base_url=%s caller=%s",
                 tier, model, base_url, caller)
        if caller.startswith("main."):
            return
        from telemetry import RoutingDecision, write_routing_decision
        rd = RoutingDecision(
            tier_chosen=str(getattr(tier, "value", tier)),
            routing_source=("inproc:" + caller)[:120],
            model_called=model,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return          # no event loop (sync caller) -- the log line still stands
        task = loop.create_task(write_routing_decision(rd))
        _ATTRIB_TASKS.add(task)
        task.add_done_callback(_attrib_done)
    except Exception as exc:
        log.debug("in-process attribution skipped: %s", exc)


def _attrib_done(task) -> None:
    """Retire a finished attribution task AND retrieve its exception.

    Retrieving matters: a bare discard leaves the exception unconsumed, and asyncio then
    prints "Task exception was never retrieved" with a full traceback into the Router log.
    A telemetry hiccup would look like a router fault to whoever reads that log next --
    which is the exact confusion this whole packet exists to remove.
    """
    _ATTRIB_TASKS.discard(task)
    try:
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.debug("in-process attribution write failed: %s", exc)
    except Exception:
        pass


# 2026-09-06 RUNAWAY BACKSTOP for tools-bearing decodes.
# Deliberately far above any legitimate tool answer: this is not a ceiling on output, it is a
# bound on a decode that never emits a stop token. See the note in ollama_chat_completion.
_RUNAWAY_BACKSTOP = 8192

# 2026-09-17 GEMMA4 PREFACE FLOOR. gemma4 emits hundreds of undecodable tokens before its first visible
# character (measured on Forge: 184-958 tokens on a ~9k prompt, and 6 of 6 replies EMPTY at num_predict 256,
# 0 of 6 empty at 1536). A ceiling below that preface returns 0 chars, which the degenerate guard reports as
# a wedged backend -- a healthy model, a caller-side cap. This does NOT reopen the 2026-06-10 policy: a
# client's max_tokens is still dropped, nothing new is sent, and a ceiling already on the wire is only ever
# RAISED, never lowered or introduced.
_PREFACE_FLOOR = 1024
_PREFACE_FLOOR_MODELS = ("gemma4",)


def _preface_floor(model, value):
    """Raise an already-present num_predict to the floor for gemma4 models. None stays None."""
    if value is None:
        return None
    if any(k in (model or "").lower() for k in _PREFACE_FLOOR_MODELS):
        return max(value, _PREFACE_FLOOR)
    return value


async def ollama_chat_completion(
    messages: list[dict],
    tier: Tier,
    stream: bool = True,
    timeout: int | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    think: bool | None = None,
    tools: list | None = None,
    tool_choice: str | dict | None = None,
    format: str | dict | None = None,
    hard_num_predict: int | None = None,
    logprobs: bool = False,
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send a chat completion request to Ollama's OpenAI-compatible API.

    format: Ollama's grammar-constrained output mode ("json", or a JSON schema dict).
        Opt-in and None by default -- no tier changes behavior unless it asks.
        Callers that need machine-parseable output should pass it rather than
        hoping a sampled model closes its braces (see the decomposer, which failed
        JSON on 3 of 4 attempts at temperature 0.7 without it).

    Returns:
        If stream=False: Complete response dict (OpenAI format).
        If stream=True: AsyncIterator yielding SSE data lines.
    """
    backend = TIER_BACKENDS[tier]
    base_url = backend.get("base_url", settings.ollama_base_url)
    model = backend["model"]
    url = f"{base_url}/v1/chat/completions"

    # PACKET-67: attribute this dispatch before issuing it. Placed here, after the tier is
    # resolved and before any network call, so a request that hangs or fails is still
    # attributed -- the loads being chased were visible on Forge precisely because a model
    # got loaded, which happens whether or not the call goes on to succeed.
    _record_inproc_dispatch(tier, model, base_url, _inproc_caller())

    # ROUTER-NUMCTX (2026-05-29): size the context window per tier. Without an
    # explicit options.num_ctx, Ollama falls back to the model's Modelfile
    # default (2048 fleet-wide), which silently truncates long agent prompts and
    # returns empty completions (root cause that broke Continue). Per-tier value
    # comes from fleet_config.json via TIER_BACKENDS; settings.default_num_ctx is
    # the safe fallback. An explicit caller-supplied options.num_ctx always wins.
    _caller_opts = kwargs.get("options") or {}
    if _caller_opts.get("num_ctx") is not None:
        num_ctx = _caller_opts["num_ctx"]
    else:
        num_ctx = backend.get("num_ctx", settings.default_num_ctx)

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    if max_tokens is not None:
        # METRIC ONLY — never enforce as a ceiling on local models (2026-06-10).
        # Ollama post-0.30 treats max_tokens/num_predict as a hard truncation
        # limit, cutting off tool_calls and long responses mid-stream.
        # max_tokens is preserved in the Router for telemetry; stripped here.
        # See FINDING-no-maxtokens-local-models-2026-06-05.
        pass  # payload["max_tokens"] deliberately omitted
    # v114 tools passthrough — pure passthrough per Phase 0 capability matrix.
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if "options" in kwargs and kwargs["options"]:
        opts = dict(kwargs["options"])
        # Safety net: some legacy callers stuffed `think` inside `options`.
        # That's the wrong shape for Ollama (think is top-level, not nested),
        # so promote it back to top-level if the caller didn't set it explicitly.
        if "think" in opts and think is None:
            think = opts.pop("think")
    else:
        opts = {}
    # Carry num_ctx in options for the /v1 passthrough path. setdefault preserves
    # an explicit caller value (never override it, per ROUTER-NUMCTX contract).
    if opts.get("num_predict") is not None:
        opts["num_predict"] = _preface_floor(model, opts["num_predict"])
    opts.setdefault("num_ctx", num_ctx)
    if opts:
        payload["options"] = opts
    # Scalar mirror: the native /api/chat builders rebuild options from scratch
    # and would otherwise drop num_ctx. They copy this key explicitly.
    payload["num_ctx"] = num_ctx
    if think is not None:
        payload["think"] = think  # top-level Ollama field (not nested in options)
    if format is not None:
        # Grammar-constrained decoding. Top-level Ollama field, like `think`.
        # The native /api/chat builders rebuild the payload from scratch, so they
        # copy this key explicitly -- see _request_ollama / _stream_ollama_native_chat.
        payload["format"] = format
    # 2026-08-25 HARD CAP, opt-in. NOT the same thing as max_tokens, which is dropped
    # above by deliberate policy (METRIC ONLY, 2026-06-10) and must stay dropped. This
    # exists because a grammar-constrained decode with no ceiling ran 17,223 tokens
    # without emitting a stop token and was cut at 288.0s with HTTP 500, 6 times of 6.
    # Only the decomposer sets it; every other caller is unchanged. The native builders
    # rebuild native_payload from scratch, so they copy this key EXPLICITLY -- an
    # un-copied key is silently dropped, which is how tools, num_ctx and format were each
    # lost before.
    # 2026-09-06 RUNAWAY BACKSTOP. This does NOT reopen the 2026-06-10 policy above: a CLIENT's
    # max_tokens is still dropped, and _RUNAWAY_BACKSTOP is set far higher than any real tool
    # response, so nothing legitimate is truncated. It exists because a tools-bearing decode with
    # NO ceiling ran 16,395 tokens without emitting a stop token -- gpt-oss-igor-65k driven by
    # opencode, 9m44s wall clock, HTTP 200 -- on the /v1 streaming path, the one path where
    # neither ceiling reached the model. Same failure the hard cap above was built for (17,223
    # tokens, the decomposer); that caller opted in and this one never did.
    # An explicit caller value ALWAYS wins, so the decomposer's 2048 is untouched.
    # 2026-10-01: the backstop now covers TOOLS-FREE decodes too. It was gated on `tools`
    # because both recorded runaways carried tools or a grammar (16,395 tokens tools-bearing;
    # 17,223 grammar-constrained) -- absence of an incident on the plain path, not absence of
    # exposure. Measured 2026-10-01: tools-free bifrost-auto -> 1a-hearth returned 4,555
    # tokens against a client max_tokens of 1, with NO ceiling on the wire at all.
    # This does NOT reopen the 2026-06-10 policy. The CLIENT's max_tokens is still dropped
    # and still never reaches the model; 8192 sits far above any legitimate answer; and
    # _preface_floor below still only RAISES. An explicit caller value always wins, so the
    # decomposer's 2048 is untouched.
    if hard_num_predict is None:
        hard_num_predict = _RUNAWAY_BACKSTOP
    hard_num_predict = _preface_floor(model, hard_num_predict)
    if hard_num_predict is not None:
        payload["hard_num_predict"] = hard_num_predict

    # LOGPROBS (2026-08-31). Verified live against ollama 0.32.15 (Forge) AND 0.30.6
    # (Bifrost, the oldest instance in the fleet): both return per-token logprobs on
    # /api/chat and /v1. The Router simply never asked.
    #
    # Copied into the NON-STREAM builder only. The streaming builder would silently drop
    # it -- the failure mode this file warns about for tools, num_ctx and format -- so
    # rather than drop it quietly, refuse it loudly here and clear the flag.
    if logprobs and stream:
        log.warning("logprobs requested with stream=True; the native stream translator does not relay them. Ignoring. Use stream=False (fan-out already does).")
        logprobs = False
    if logprobs:
        payload["logprobs"] = True

    # A2 KEEP-ALIVE (2026-06-13): per-tier keep_alive override from fleet_config.
    # T2 (gemma4 on Forge) sets _keep_alive_override="0" so Ollama unloads the
    # model immediately after the response instead of holding it for the global
    # OLLAMA_KEEP_ALIVE window (30m on Forge) -- a held T2 slot squats VRAM and
    # blocks the toolmaster/Kilo co-load. keep_alive is a top-level Ollama field
    # (Go duration string or seconds: "0"=unload now, "30s", -1=keep forever).
    # The native /api/chat builders rebuild their payload from scratch and copy
    # this key explicitly (see _request_ollama / _stream_ollama_native_chat).
    # Tiers without the override (toolmaster, T1) keep Ollama's default behavior.
    _keep_alive = backend.get("_keep_alive_override")
    if _keep_alive is not None:
        payload["keep_alive"] = _keep_alive

    if stream:
        # When the caller requested think=False (or True), Ollama only honors
        # the field on the native /api/chat endpoint -- the /v1/chat/completions
        # OpenAI-compat shim ignores it. Route streaming through /api/chat in
        # that case and translate NDJSON to OpenAI SSE on the fly.
        #
        # ROUTER-NUMCTX (2026-05-29): the same /v1 shim also ignores nested
        # options, so options.num_ctx set above would NOT take effect there --
        # this is exactly the path that truncated Continue (non-thinking model,
        # plain streaming chat). Route plain streaming chat (no think, no tools)
        # through native /api/chat too, so num_ctx is actually honored. Tool
        # requests stay on the /v1 passthrough: the native stream translator does
        # not relay tool_calls, so moving them would regress streaming tool use.
        #
        # WEDGE CHECK (#98) wraps BOTH streaming shapes. #87 guarded only the non-streaming
        # paths, so a wedged instance was refused at stream=False and delivered verbatim at
        # stream=True -- which of those a caller got depended on nothing but that flag.
        # empty_is_wedged=True on the CHAT shapes ONLY: an assistant turn with no content
        # and no tool call is a void, not an answer. Deliberately NOT set on the
        # /api/generate FIM path below, where completing to nothing is legitimate and a
        # refusal would turn 'no suggestion' into a 502 in the editor. guard_stream
        # tracks tool-call chunks itself, so the tools branch below stays safe.
        if think is not None or (tools is None and tool_choice is None):
            return guard_stream(_stream_ollama_native_chat(url, payload),
                                endpoint=url, model=model, empty_is_wedged=True)
        return guard_stream(_stream_ollama(url, payload), endpoint=url, model=model,
                            empty_is_wedged=True)
    else:
        return await _request_ollama(url, payload, timeout=timeout, tier=tier)


async def ollama_completion(
    prompt: str,
    tier: Tier,
    stream: bool = True,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send a raw completion request (for autocomplete / FIM).
    Uses Ollama's /api/generate endpoint since /v1/completions
    support varies. Translates response to OpenAI format.
    """
    backend = TIER_BACKENDS[tier]
    base_url = backend.get("base_url", settings.ollama_base_url)
    model = backend["model"]
    url = f"{base_url}/api/generate"

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "temperature": temperature,
        },
    }
    if max_tokens is not None:
        # METRIC ONLY — see ollama_chat_completion for rationale (2026-06-10).
        pass  # payload["options"]["num_predict"] deliberately omitted
    # ROUTER-NUMCTX (2026-05-29): size context window for the generate path too,
    # so FIM/autocomplete prompts are not truncated by the Modelfile default.
    # Per-tier value from fleet_config; settings.default_num_ctx fallback; an
    # explicit caller options.num_ctx wins.
    _caller_opts = kwargs.get("options") or {}
    if _caller_opts.get("num_ctx") is not None:
        payload["options"]["num_ctx"] = _caller_opts["num_ctx"]
    else:
        payload["options"]["num_ctx"] = backend.get("num_ctx", settings.default_num_ctx)

    if stream:
        # WEDGE CHECK (#98). This shape carries its text as choices[0].text rather than a
        # delta; guard_stream reads both, so FIM/autocomplete is covered and not merely
        # assumed to be.
        return guard_stream(_stream_ollama_generate(url, payload, model),
                            endpoint=url, model=model)
    else:
        return await _request_ollama_generate(url, payload, model)


def strip_think_blocks(text: str) -> str:
    """Remove <think>...</think> blocks from model output.

    R1 and Qwen3.5 in thinking mode emit reasoning inside <think> tags.
    Strip these before returning to the user in INTERACTIVE mode.
    Decomposer calls should NOT strip — the reasoning improves DAG quality.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Internal: streaming
# ---------------------------------------------------------------------------

async def _stream_ollama(url: str, payload: dict) -> AsyncIterator[str]:
    """Stream SSE from Ollama's /v1/chat/completions (already OpenAI format)."""
    # F1 (2026-08-28): this path RELAYS ollama's SSE verbatim, so usage cannot be added
    # downstream -- ollama has to be asked. Verified live with a control: with the option
    # a usage frame arrives, without it zero frames do. Copy rather than mutate the
    # caller's payload; this must not leak onto the non-streaming builders.
    payload = dict(payload)
    payload["stream_options"] = {"include_usage": True}
    # 2026-09-06: /v1 is the ONE path where neither ceiling reaches the model. The client's
    # max_tokens is dropped by policy (METRIC ONLY, 2026-06-10) and hard_num_predict is copied
    # ONLY into the two native builders, so here it rides along as a key ollama's OpenAI-compat
    # endpoint silently ignores -- a no-op exactly like the un-copied keys the comments above
    # warn about. Translate it to the field this endpoint DOES honor, and drop the native-only
    # key rather than sending it as junk.
    _hnp = payload.pop("hard_num_predict", None)
    if _hnp is not None:
        payload["max_tokens"] = _hnp
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"
                elif line.strip() == "":
                    continue


def _native_messages(messages: list[dict]) -> list[dict]:
    """Translate OpenAI-shaped tool_calls into what Ollama's native /api/chat accepts.

    OpenAI serializes `tool_calls[].function.arguments` as a JSON STRING. Ollama /api/chat wants an
    OBJECT and rejects the string, which the Router reports as `502 All tiers failed`. Without
    this, every multi-turn native-tool conversation fails on the turn that carries the tool result
    (measured 24/24, 2026-08-29). igor_tool_loop -- the loop Kilo runs on -- is built entirely on
    native tool_calls, so this is what stops a Kilo-style stack routing through the Router.

    Minimal by intent: ONLY `arguments`, and only when it is a string parsing to an object. `id`,
    `type`, `tool_call_id` and `name` are left alone -- Ollama accepted all of them in testing, and
    transforming more would change behavior that has not been measured. A string that does not
    parse is passed through untouched, so a genuinely malformed payload still earns Ollama's own
    error instead of a silent rewrite here.

    Copies only the messages it changes; the rest pass by reference, so this is cheap on the common
    path where nothing carries tool_calls. The caller's list is never mutated.
    """
    out = []
    for m in messages:
        tcs = m.get("tool_calls") if isinstance(m, dict) else None
        if not tcs:
            out.append(m)
            continue
        new_tcs, touched = [], False
        for tc in tcs:
            fn = (tc or {}).get("function")
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (ValueError, TypeError):
                    new_tcs.append(tc)
                    continue
                if isinstance(parsed, dict):
                    tc = dict(tc)
                    tc["function"] = dict(fn)
                    tc["function"]["arguments"] = parsed
                    touched = True
            new_tcs.append(tc)
        if touched:
            m = dict(m)
            m["tool_calls"] = new_tcs
        out.append(m)
    return out


def _openai_tool_calls(tool_calls: list) -> list:
    """Translate Ollama native /api/chat tool_calls into the OpenAI response shape.

    The inverse of _native_messages. Ollama emits `function.arguments` as an OBJECT and puts
    `index` inside `function`, with no `type`. OpenAI clients need `arguments` as a JSON STRING
    and `type: "function"` -- the ai-sdk client Kilo uses, and anthropic_compat.py's own
    json.loads(arguments), which raises TypeError on a dict. Measured 2026-09-11: the non-stream
    path returned the native shape verbatim, while the streaming path and Ollama's own /v1 were
    spec-correct.

    A string `arguments` passes through untouched; a missing id gets one, as Ollama's /v1 does.
    igor_tool_loop and bifrost_delegate already accept both shapes, so they are unaffected.
    """
    out = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args if args is not None else {}, separators=(",", ":"))
        out.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args}})
    return out


async def _stream_ollama_native_chat(url: str, payload: dict) -> AsyncIterator[str]:
    """Stream from Ollama /api/chat (which honors `think`) and translate the
    NDJSON line stream to OpenAI chat-completion SSE chunks. Used whenever a
    caller explicitly set the think flag, since /v1/chat/completions ignores it.
    """
    native_url = url.replace("/v1/chat/completions", "/api/chat")
    native_payload = {
        "model": payload["model"],
        # tool_call arguments arrive as an OpenAI JSON STRING; /api/chat needs an OBJECT
        "messages": _native_messages(payload["messages"]),
        "stream": True,
    }
    if "temperature" in payload:
        native_payload.setdefault("options", {})["temperature"] = payload["temperature"]
    if "max_tokens" in payload and payload["max_tokens"]:
        native_payload.setdefault("options", {})["num_predict"] = \
            _preface_floor(payload["model"], payload["max_tokens"])
    # ROUTER-NUMCTX (2026-05-29): carry the per-tier context window onto the
    # native /api/chat request (the OpenAI-compat shim drops nested options).
    if payload.get("num_ctx") is not None:
        native_payload.setdefault("options", {})["num_ctx"] = payload["num_ctx"]
    # 2026-08-25: the opt-in hard ceiling (see ollama_chat_completion). BOTH builders get
    # it -- the decomposer is stream=False, but a cap that applied on only one path is
    # exactly the class of bug the comments here already warn about.
    if payload.get("hard_num_predict") is not None:
        native_payload.setdefault("options", {})["num_predict"] = \
            _preface_floor(payload["model"], payload["hard_num_predict"])
    if "think" in payload and payload["think"] is not None:
        native_payload["think"] = payload["think"]
    # Grammar-constrained output (v218). Same class of bug as tools and num_ctx:
    # this builder rebuilds native_payload from scratch, so an un-copied key is
    # silently dropped and the caller gets free-sampled output while believing it
    # asked for constrained JSON.
    if payload.get("format") is not None:
        native_payload["format"] = payload["format"]
    # A2 KEEP-ALIVE (2026-06-13): forward the per-tier keep_alive override onto
    # the native /api/chat request (top-level field, not nested in options).
    # See ollama_chat_completion for rationale (T2 unload-after-use).
    if payload.get("keep_alive") is not None:
        native_payload["keep_alive"] = payload["keep_alive"]

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = payload["model"]
    role_sent = False
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        async with client.stream("POST", native_url, json=native_payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") or {}
                token = msg.get("content", "")
                done = obj.get("done", False)
                if not role_sent:
                    role_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(role_chunk)}\n\n"
                    role_sent = True
                if token:
                    delta_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(delta_chunk)}\n\n"
                if done:
                    final_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": obj.get("done_reason", "stop")}],
                    }
                    # F1 (2026-08-28): report usage. Ollama puts prompt_eval_count /
                    # eval_count on this same final line, so this is free. Without it
                    # usage_split() correctly recorded (0,0) and 90.5% of rows had no
                    # input token count. METRIC ONLY -- adds no ceiling of any kind.
                    final_chunk["usage"] = {
                        "prompt_tokens": obj.get("prompt_eval_count", 0),
                        "completion_tokens": obj.get("eval_count", 0),
                        "total_tokens": (obj.get("prompt_eval_count", 0)
                                         + obj.get("eval_count", 0)),
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return


async def _stream_ollama_generate(
    url: str, payload: dict, model: str
) -> AsyncIterator[str]:
    """
    Stream from Ollama's /api/generate and translate to OpenAI completion SSE format.
    Ollama /api/generate returns JSON lines: {"response": "token", "done": false}
    We translate to: data: {"choices": [{"text": "token", ...}]}
    """
    completion_id = f"cmpl-{uuid.uuid4().hex[:12]}"
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if chunk.get("done"):
                    # Final chunk
                    sse_data = json.dumps({
                        "id": completion_id,
                        "object": "text_completion",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "text": "",
                            "finish_reason": "stop",
                        }],
                    })
                    # F1 (2026-08-28): attach usage to the final chunk. Reopened rather
                    # than restating the literal above, so this edit does not depend on that
                    # dict's shape. METRIC ONLY.
                    _fin = json.loads(sse_data)
                    _fin["usage"] = {
                        "prompt_tokens": chunk.get("prompt_eval_count", 0),
                        "completion_tokens": chunk.get("eval_count", 0),
                        "total_tokens": (chunk.get("prompt_eval_count", 0)
                                         + chunk.get("eval_count", 0)),
                    }
                    yield f"data: {json.dumps(_fin)}\n\n"
                    yield "data: [DONE]\n\n"
                else:
                    token = chunk.get("response", "")
                    if token:
                        sse_data = json.dumps({
                            "id": completion_id,
                            "object": "text_completion",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "text": token,
                                "finish_reason": None,
                            }],
                        })
                        yield f"data: {sse_data}\n\n"


# ---------------------------------------------------------------------------
# Internal: non-streaming
# ---------------------------------------------------------------------------

async def _request_ollama(url: str, payload: dict, timeout: int | None = None, tier: Tier | None = None) -> dict:
    """Non-streaming request to Ollama. Uses /api/chat (native) to capture eval timing,
    then translates to OpenAI format. Falls back to /v1/chat/completions if native fails."""
    _timeout = timeout if timeout is not None else settings.ollama_timeout_by_tier.get(
        getattr(tier, "value", None), settings.ollama_timeout)

    # Use native /api/chat for eval_count/eval_duration (tok/s)
    native_url = url.replace("/v1/chat/completions", "/api/chat")
    native_payload = {
        "model": payload["model"],
        # tool_call arguments arrive as an OpenAI JSON STRING; /api/chat needs an OBJECT
        "messages": _native_messages(payload["messages"]),
        "stream": False,
    }
    if "temperature" in payload:
        native_payload.setdefault("options", {})["temperature"] = payload["temperature"]
    if "max_tokens" in payload and payload["max_tokens"]:
        native_payload.setdefault("options", {})["num_predict"] = \
            _preface_floor(payload["model"], payload["max_tokens"])
    # ROUTER-NUMCTX (2026-05-29): carry the per-tier context window onto the
    # native /api/chat request (the OpenAI-compat shim drops nested options).
    if payload.get("num_ctx") is not None:
        native_payload.setdefault("options", {})["num_ctx"] = payload["num_ctx"]
    # 2026-08-25: the opt-in hard ceiling (see ollama_chat_completion). BOTH builders get
    # it -- the decomposer is stream=False, but a cap that applied on only one path is
    # exactly the class of bug the comments here already warn about.
    if payload.get("hard_num_predict") is not None:
        native_payload.setdefault("options", {})["num_predict"] = \
            _preface_floor(payload["model"], payload["hard_num_predict"])
    if "think" in payload and payload["think"] is not None:
        native_payload["think"] = payload["think"]
    # Grammar-constrained output (v218). Same class of bug as tools and num_ctx:
    # this builder rebuilds native_payload from scratch, so an un-copied key is
    # silently dropped and the caller gets free-sampled output while believing it
    # asked for constrained JSON.
    if payload.get("format") is not None:
        native_payload["format"] = payload["format"]
    # A2 KEEP-ALIVE (2026-06-13): forward the per-tier keep_alive override onto
    # the native /api/chat request (top-level field, not nested in options).
    # See ollama_chat_completion for rationale (T2 unload-after-use).
    if payload.get("keep_alive") is not None:
        native_payload["keep_alive"] = payload["keep_alive"]
    # LOGPROBS (2026-08-31): same class as tools/num_ctx/format above -- this builder
    # rebuilds native_payload from scratch, so an un-copied key is silently dropped and
    # the caller gets no logprobs while believing it asked for them.
    if payload.get("logprobs"):
        native_payload["logprobs"] = True
    # v114.5 (2026-05-19): forward tools/tool_choice through native /api/chat.
    # v114-tools-passthrough wired these at the outer OpenAI-compat payload but
    # _request_ollama rebuilds native_payload from scratch and dropped them.
    if payload.get("tools") is not None:
        native_payload["tools"] = payload["tools"]
    if payload.get("tool_choice") is not None:
        native_payload["tool_choice"] = payload["tool_choice"]

    # R-18 part 2 (#120): serialize NPU traffic AND survive a canceled caller. Both halves
    # are NO-OPs for every non-NPU url -- the ordinary Ollama hosts (Bifrost/Hearth :11434)
    # are untouched, stay fully concurrent, and still die with an abandoning client, which
    # for them is the right behavior.
    #
    # The AsyncClient is built INSIDE _send rather than around it. npu_uncancellable runs
    # _send in a shielded task, so if the caller's frame owned the client, unwinding that
    # frame on abort would close the connection pool and kill the "shielded" request anyway
    # -- the shield would be decorative. Same shape as backends/openai_compat.py::_request,
    # which #94 fixed; THIS call site is the residual that commit left open.
    async def _send() -> dict:
        async with httpx.AsyncClient(timeout=_timeout) as client:
            response = await client.post(native_url, json=native_payload)
            response.raise_for_status()
            return response.json()

    data = await npu_uncancellable(native_url, _send)

    # Extract timing for tok/s gauge
    eval_count = data.get("eval_count", 0)
    eval_duration = data.get("eval_duration", 0)
    tok_per_s = None
    if eval_count and eval_duration and eval_duration > 0:
        tok_per_s = eval_count / (eval_duration / 1_000_000_000)
        try:
            from metrics import TIER_TOK_S
            # ROUTER-T (2026-05-29): label by Tier value -- the Portal queries
            # bifrost_tier_tok_s by tier ("2","2.5",...), not by model name.
            _label = tier.value if tier is not None else payload.get("model", "")
            TIER_TOK_S.labels(tier=_label).set(round(tok_per_s, 1))
        except Exception:
            pass

    # Translate to OpenAI format. Preserve `thinking` side-channel from Ollama
    # native /api/chat so coalesce_reasoning_into_content can promote it when
    # content arrives empty under constrained max_tokens (v91 fix).
    _msg = data.get("message", {}) or {}
    content = _msg.get("content", "")
    _shaped_msg = {"role": "assistant", "content": content}
    if isinstance(_msg.get("thinking"), str) and _msg["thinking"].strip():
        _shaped_msg["thinking"] = _msg["thinking"]
    # v114.5 (2026-05-19): preserve native tool_calls through OpenAI-compat
    # response shaping. Ollama /api/chat emits message.tool_calls when caller
    # provided tools[]; missing extraction here was the second half of v114
    # Test 3 PARTIAL (the first half was the Igor overlay reshaping content).
    if _msg.get("tool_calls"):
        # 2026-09-11: in the OpenAI shape (string arguments, type "function"); see _openai_tool_calls.
        _shaped_msg["tool_calls"] = _openai_tool_calls(_msg["tool_calls"])
    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload["model"],
        "choices": [{
            "index": 0,
            "message": _shaped_msg,
            # Ceremony 4 P4 (2026-09-11): report a length stop instead of hardcoding "stop".
            # The streaming path (above) already forwards done_reason; the tool loop needs it
            # to refuse tool calls whose arguments were cut off at num_predict.
            # 2026-09-12 (John): "tool_calls" when the reply carries tool calls, per the OpenAI spec.
            # "length" still wins: the tool loop's P4 guard must see a reply cut off at num_predict.
            "finish_reason": ("length" if data.get("done_reason") == "length"
                              else "tool_calls" if _shaped_msg.get("tool_calls") else "stop"),
        }],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": eval_count,
            "total_tokens": data.get("prompt_eval_count", 0) + eval_count,
        },
        "_bifrost_tok_s": tok_per_s,
    }

    # LOGPROB AGGREGATE (2026-08-31). Attached the same way as _bifrost_tok_s: a private
    # key on the OpenAI-shaped response, invisible to ordinary callers.
    #
    # float() is NOT decoration. Measured on a live 24-token reply: a token whose logprob
    # is exactly 0 arrives as JSON int and every other token as float, MIXED IN THE SAME
    # RESPONSE (8 int, 16 float). Without the coercion this does integer arithmetic on part
    # of the sequence.
    #
    # min is carried beside avg because the distribution is heavily skewed toward 0: a mean
    # is diluted by confident filler, so the worst token may predict quality better. Which
    # one actually does is a tuning question and both are recorded rather than assumed.
    _lp_raw = data.get("logprobs")
    if isinstance(_lp_raw, list) and _lp_raw:
        _vals = []
        for _e in _lp_raw:
            if isinstance(_e, dict) and _e.get("logprob") is not None:
                try:
                    _vals.append(float(_e["logprob"]))
                except (TypeError, ValueError):
                    pass
        if _vals:
            response["_bifrost_logprobs"] = {
                "avg": sum(_vals) / len(_vals),
                "min": min(_vals),
                "count": len(_vals),
            }
    from reasoning_coalesce import coalesce_reasoning_into_content
    _shaped = coalesce_reasoning_into_content(response)
    # WEDGE CHECK (#87). Judged AFTER coalescing, so this evaluates the answer the caller
    # actually receives rather than an intermediate form with empty content.
    # empty_is_wedged=True: this is the CHAT path, where an assistant turn with no
    # content and no tool call is a void, not an answer. The /api/generate check below
    # deliberately stays at the default -- completing to nothing is legitimate there.
    check_openai_response(_shaped, endpoint=native_url, model=payload.get("model"), empty_is_wedged=True)
    return _shaped


async def _request_ollama_generate(
    url: str, payload: dict, model: str
) -> dict:
    """Non-streaming request to Ollama /api/generate, translated to OpenAI format."""
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    _resp = {
        "id": f"cmpl-{uuid.uuid4().hex[:12]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "text": data.get("response", ""),
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
        },
    }
    # WEDGE CHECK (#87) -- /api/generate reports eval_count too, so both signals apply.
    check_openai_response(_resp, endpoint=url, model=model)
    return _resp
