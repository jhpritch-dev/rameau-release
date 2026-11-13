"""
BIFROST Router — Ollama Backend
================================
Proxies OpenAI-compatible requests to local Ollama.
Ollama natively serves /v1/chat/completions, so this is
mostly a pass-through with model override and streaming relay.
"""

import json
import time
import uuid
from typing import AsyncIterator

import httpx

from config import Tier, TIER_BACKENDS, settings


async def ollama_chat_completion(
    messages: list[dict],
    tier: Tier,
    stream: bool = True,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send a chat completion request to Ollama's OpenAI-compatible API.

    Returns:
        If stream=False: Complete response dict (OpenAI format).
        If stream=True: AsyncIterator yielding SSE data lines.
    """
    backend = TIER_BACKENDS[tier]
    base_url = backend.get("base_url", settings.ollama_base_url)
    model = backend["model"]
    url = f"{base_url}/v1/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    if stream:
        return _stream_ollama(url, payload)
    else:
        return await _request_ollama(url, payload)


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
        payload["options"]["num_predict"] = max_tokens

    if stream:
        return _stream_ollama_generate(url, payload, model)
    else:
        return await _request_ollama_generate(url, payload, model)


# ---------------------------------------------------------------------------
# Internal: streaming
# ---------------------------------------------------------------------------

async def _stream_ollama(url: str, payload: dict) -> AsyncIterator[str]:
    """Stream SSE from Ollama's /v1/chat/completions (already OpenAI format)."""
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield line + "\n\n"
                elif line.strip() == "":
                    continue


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
                    yield f"data: {sse_data}\n\n"
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

async def _request_ollama(url: str, payload: dict) -> dict:
    """Non-streaming request to Ollama /v1/chat/completions."""
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def _request_ollama_generate(
    url: str, payload: dict, model: str
) -> dict:
    """Non-streaming request to Ollama /api/generate, translated to OpenAI format."""
    async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()

    return {
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
