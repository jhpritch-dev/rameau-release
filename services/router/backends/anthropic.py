"""
BIFROST Router — Anthropic Backend
====================================
Translates OpenAI-compatible chat requests to Anthropic Messages API
format, and translates responses back. Supports streaming.

This handles the format gap so Continue.dev (OpenAI-speaking) can
transparently hit Claude when the Router escalates to Tier 3.
"""

import json
import os
import time
import uuid
from typing import AsyncIterator

import httpx

from config import settings


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


def _get_api_key() -> str:
    """Resolve API key from settings or environment."""
    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError(
            "Anthropic API key not configured. Set ANTHROPIC_API_KEY env var "
            "or configure in router settings."
        )
    return key


def _translate_messages(openai_messages: list[dict]) -> tuple[str | None, list[dict]]:
    """
    Convert OpenAI message format to Anthropic format.

    OpenAI: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    Anthropic: system="...", messages=[{"role": "user", "content": "..."}]

    Returns:
        (system_prompt, anthropic_messages)
    """
    system_prompt = None
    messages = []

    for msg in openai_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # Anthropic uses a separate system parameter
            if system_prompt is None:
                system_prompt = content
            else:
                system_prompt += "\n\n" + content
        elif role == "assistant":
            messages.append({"role": "assistant", "content": content})
        else:
            # user, tool, function → treat as user
            messages.append({"role": "user", "content": content})

    # Anthropic requires messages to start with user role
    # and alternate user/assistant. Merge consecutive same-role messages.
    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(msg)

    # Ensure first message is from user
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "Hello."})

    return system_prompt, merged


def _anthropic_to_openai_response(data: dict, model: str) -> dict:
    """Convert Anthropic response to OpenAI chat completion format."""
    content_blocks = data.get("content", [])
    text = "".join(
        block.get("text", "")
        for block in content_blocks
        if block.get("type") == "text"
    )

    usage = data.get("usage", {})

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
            },
            "finish_reason": _map_stop_reason(data.get("stop_reason")),
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def _map_stop_reason(reason: str | None) -> str:
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
    }
    return mapping.get(reason, "stop")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def anthropic_chat_completion(
    messages: list[dict],
    stream: bool = True,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    **kwargs,
) -> dict | AsyncIterator[str]:
    """
    Send chat completion via Anthropic Messages API.
    Accepts OpenAI-format messages, returns OpenAI-format response.
    """
    api_key = _get_api_key()
    target_model = model or settings.anthropic_model
    system_prompt, anthropic_messages = _translate_messages(messages)

    payload = {
        "model": target_model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    if stream:
        return _stream_anthropic(headers, payload, target_model)
    else:
        return await _request_anthropic(headers, payload, target_model)


# ---------------------------------------------------------------------------
# Internal: streaming
# ---------------------------------------------------------------------------

async def _stream_anthropic(
    headers: dict, payload: dict, model: str
) -> AsyncIterator[str]:
    """
    Stream from Anthropic SSE and translate to OpenAI SSE format.

    Anthropic events:
        message_start, content_block_start, content_block_delta,
        content_block_stop, message_delta, message_stop

    OpenAI format:
        data: {"choices": [{"delta": {"content": "token"}}]}
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    async with httpx.AsyncClient(timeout=settings.anthropic_timeout) as client:
        async with client.stream(
            "POST", ANTHROPIC_API_URL, json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise httpx.HTTPStatusError(
                    f"Anthropic API error: {response.status_code} {body.decode()}",
                    request=response.request,
                    response=response,
                )

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break

                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        openai_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"

                elif event_type == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason:
                        openai_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": _map_stop_reason(stop_reason),
                            }],
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"
                        yield "data: [DONE]\n\n"

                elif event_type == "message_stop":
                    # Belt and suspenders — also emit [DONE] here
                    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Internal: non-streaming
# ---------------------------------------------------------------------------

async def _request_anthropic(
    headers: dict, payload: dict, model: str
) -> dict:
    """Non-streaming request to Anthropic, translated to OpenAI format."""
    payload["stream"] = False

    async with httpx.AsyncClient(timeout=settings.anthropic_timeout) as client:
        response = await client.post(
            ANTHROPIC_API_URL, json=payload, headers=headers
        )
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"Anthropic API error: {response.status_code} {response.text}",
                request=response.request,
                response=response,
            )
        data = response.json()

    return _anthropic_to_openai_response(data, model)
