"""
BIFROST Router — OpenAI-Compatible Backend
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

from config import settings


# ---------------------------------------------------------------------------
# Provider configs (resolved at call time from env vars)
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
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

    if not api_key:
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
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send chat completion to any OpenAI-compatible provider.
    Returns OpenAI-format response (native — no translation needed).
    """
    base_url, api_key, default_model = _resolve_provider(provider)
    target_model = model or default_model
    url = f"{base_url}/chat/completions"

    payload = {
        "model": target_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = getattr(settings, f"{provider}_timeout", 60)

    if stream:
        return _stream(url, headers, payload, timeout)
    else:
        return await _request(url, headers, payload, timeout)


# ---------------------------------------------------------------------------
# Internal: streaming
# ---------------------------------------------------------------------------

async def _stream(
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
    """Non-streaming request to an OpenAI-compatible endpoint."""
    payload["stream"] = False
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"API error: {response.status_code} {response.text[:500]}",
                request=response.request,
                response=response,
            )
        return response.json()
