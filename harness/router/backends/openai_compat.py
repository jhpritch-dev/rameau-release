"""
BIFROST Router -- OpenAI-Compatible Backend
============================================
Generic backend for any provider with an OpenAI-compatible API.
Works for: Gemini, Groq, OpenRouter, Together, and others.

Each provider just needs a base_url and api_key.
"""

import json
import os
import time
import uuid
from typing import AsyncIterator

import httpx

from degenerate import check_openai_response, guard_stream

from npu_gate import npu_uncancellable

from config import settings
from ._retry import retry_stream, with_retries


# ---------------------------------------------------------------------------
# Provider configs (resolved at call time from env vars)
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    # lemonade entry removed 2026-06-20 — Forge :8000 fossil (never listening; not wired into any tier;
    # the NPU classifier is flm :8003, not Lemonade). Mirrors the v154 llamaserver removal.
    #
    # NPU-TOOL-LEAF Phase 1 (2026-07-23): FastFlowLM on the Forge XDNA2 NPU. This is the
    # SAME service the note above names as the real NPU endpoint -- it is an FLM entry, NOT
    # a Lemonade revival, and stands up no second engine. Native OpenAI tool_calls arrived
    # in FLM v0.9.40 and are live on the deployed v0.9.45 (Gate 0 BRANCH A, 3/3 including
    # abstention, on qwen3.5:4b).
    #
    # env_key is DELIBERATELY empty: FLM :8003 is auth-free on the LAN. _resolve_provider
    # only raises "API key not configured" when env_key is truthy, so an empty string keeps
    # the no-auth path working instead of hard-failing every call.
    "flm": {
        "base_url": "http://127.0.0.1:8003/v1",
        "env_key": "",
        "default_model": "gemma4-it:e4b",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
    # v154 D-sweep: llamaserver entry removed — Forge :11437 killed in V152 stack cutover.
    # v115 V115-OPENROUTER-1 (2026-05-20) — cloud cascade via OpenRouter.
    # API key sourced from k8s secret openrouter-api (env OPENROUTER_API_KEY on
    # bifrost-router deployment). Bearer auth header injected by openai_compat_chat_completion.
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4-5",
    },
    # v162 T3-ESCALATION-REDESIGN (2026-06-13): capability-routed cloud tiers.
    # fleet_config tiers 3-cerebras / 3-perplexity now carry provider=cerebras /
    # perplexity so the openai_compat dispatch (backend["provider"]) resolves here.
    # Keys land via k8s secret in Phase D; env_vars startup check WARNs until then.
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "default_model": "llama-3.3-70b",
    },
    "perplexity": {
        "base_url": "https://api.perplexity.ai",
        "env_key": "PERPLEXITY_API_KEY",
        "default_model": "sonar",
    },
    # v163 COHERE-WIRING (2026-06-14): RAG-native cloud tier. OpenAI-compat path is
    # /compatibility/v1 (NOT native /v1 -- confirmed via docs.cohere.com/docs/compatibility-api).
    # default_model command-r-plus; flip to command-a-* if Cohere deprecates command-r.
    # Key via k8s secret COHERE_API_KEY (wired Phase B 2026-06-14).
    "cohere": {
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "env_key": "COHERE_API_KEY",
        "default_model": "command-r-plus",
    },
}


def _resolve_provider(provider: str) -> tuple[str, str, str]:
    """
    Resolve base_url, api_key, and default_model for a provider.
    Returns (base_url, api_key, default_model).
    """
    defaults = PROVIDER_DEFAULTS.get(provider)
    if not defaults:
        raise ValueError(f"Unknown provider: {provider}")

    base_url = defaults["base_url"]
    api_key = os.environ.get(defaults["env_key"], "")

    if not api_key:
        # Check settings object for provider-specific keys
        key_attr = f"{provider}_api_key"
        api_key = getattr(settings, key_attr, "") or ""

    if not api_key and defaults.get("env_key"):
        raise ValueError(
            f"{provider} API key not configured. "
            f"Set {defaults['env_key']} environment variable."
        )

    return base_url, api_key, defaults["default_model"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def openai_compat_chat_completion(
    messages: list[dict],
    provider: str,
    model: str | None = None,
    stream: bool = True,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    base_url: str | None = None,
    extra_body: dict | None = None,
    tools: list | None = None,
    tool_choice: str | dict | None = None,
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send chat completion to any OpenAI-compatible provider.
    Returns OpenAI-format response (native -- no translation needed).

    `extra_body` is merged into the JSON payload top-level. Used by the
    capability-aware dispatcher to inject backend-specific knobs such as
    `chat_template_kwargs={"enable_thinking": False}` for llama.cpp qwen3.
    """
    _base_url, api_key, default_model = _resolve_provider(provider)
    if base_url:
        _base_url = base_url
    target_model = model or default_model
    url = f"{_base_url}/chat/completions"

    payload = {
        "model": target_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    # v114 tools passthrough — set before extra_body merge so extra_body can
    # override only if a caller explicitly requested it.
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if extra_body:
        payload.update(extra_body)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = getattr(settings, f"{provider}_timeout", 60)

    if stream:
        # WEDGE CHECK (#98). This function serves the CLOUD providers as well, and they are
        # deliberately included: the non-streaming check already covers them (see _request),
        # and the check is about self-contradictory answers, not about which vendor sent
        # them. The cost on a healthy cloud stream is the same few tokens of hold.
        # empty_is_wedged=True (2026-10-05): this module has ONE entry point and always posts
        # `messages` to /chat/completions, so every response through here is a CHAT turn and an
        # assistant turn with no content and no tool call is a void, not an answer. ollama.py
        # has passed True on its chat paths since #98 and withheld it only from the /api/generate
        # FIM path, where completing to nothing is legitimate -- there is no such path here.
        # Until now this was passed in NEITHER direction, so the same void was refused on a local
        # chat tier and delivered as success on every CLOUD tier, decided by nothing but which
        # backend served the tier. guard_stream excuses the two legitimate empties itself: it
        # tracks tool-call chunks, and the truncation arm delivers an empty body whose backend
        # reported finish_reason "length".
        return guard_stream(_stream(url, headers, payload, timeout),
                            endpoint=url, model=target_model, empty_is_wedged=True)
    else:
        return await _request(url, headers, payload, timeout)


# ---------------------------------------------------------------------------
# Internal: streaming
# ---------------------------------------------------------------------------

async def _stream(
    url: str, headers: dict, payload: dict, timeout: int
) -> AsyncIterator[str]:
    """Stream SSE from an OpenAI-compatible endpoint, retrying transient establishment
    failures (429/503/504, timeouts) before the first chunk. Pass-through once streaming."""
    async for chunk in retry_stream(lambda: _stream_once(url, headers, payload, timeout)):
        yield chunk


async def _stream_once(
    url: str, headers: dict, payload: dict, timeout: int
) -> AsyncIterator[str]:
    """Stream SSE from an OpenAI-compatible endpoint. Pass-through."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise httpx.HTTPStatusError(
                    f"API error: {response.status_code} {body.decode()[:500]}",
                    request=response.request,
                    response=response,
                )
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"
                elif line.strip() == "":
                    continue


# ---------------------------------------------------------------------------
# Internal: non-streaming
# ---------------------------------------------------------------------------

async def _request(
    url: str, headers: dict, payload: dict, timeout: int
) -> dict:
    """Non-streaming request to an OpenAI-compatible endpoint. Retries transient cloud
    failures (429/503/504, timeouts) with bounded exponential backoff."""
    payload["stream"] = False

    async def _once() -> dict:
        # The client is created INSIDE _send, not around it. npu_uncancellable runs _send in
        # a shielded task, so an aborted caller must not own the AsyncClient -- unwinding the
        # caller's frame would close the pool and kill the "shielded" request anyway.
        async def _send() -> dict:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        f"API error: {response.status_code} {response.text[:500]}",
                        request=response.request,
                        response=response,
                    )
                return response.json()

        # R-18: serialize NPU traffic AND survive a canceled caller (#94). Both halves are
        # NO-OPs for every non-NPU url -- this same function serves all the cloud providers,
        # which must not be serialized and whose requests SHOULD die with an abandoning
        # client. npu_uncancellable holds the gate lock for the shielded request's real
        # lifetime, so releasing it early cannot let the next call in mid-prefill.
        return await npu_uncancellable(url, _send)

    data = await with_retries(_once)
    # v91: coalesce reasoning_content/reasoning into content if content is
    # empty (llama.cpp Qwen3 puts CoT in reasoning_content under constrained
    # max_tokens). Pass-through for non-thinking responses.
    from reasoning_coalesce import coalesce_reasoning_into_content
    _shaped = coalesce_reasoning_into_content(data)
    # WEDGE CHECK (#87). This function also serves every CLOUD provider, which is fine:
    # the check is about self-contradictory responses, not about which vendor sent them.
    # empty_is_wedged=True -- the non-streaming half of the same 2026-10-05 change. Judged
    # AFTER the coalesce above, so a thinking model whose output arrived in the side channel has
    # already been promoted and is not mistaken for a void. has_tool_calls is read off the
    # response by check_openai_response, so a tool call carrying no content still passes.
    check_openai_response(_shaped, endpoint=url, model=payload.get("model"),
                          empty_is_wedged=True)
    return _shaped
