import os
"""
anthropic_adapter.py — BIFROST Anthropic-Format Adapter
=======================================================
Translates Anthropic SDK wire format → BIFROST Router → Anthropic-compatible response.
Allows Claude Code (and any Anthropic SDK client) to route inference through
BIFROST's local fleet instead of calling Anthropic's API directly.

Deploy: Bifrost :8085
Usage:  Set ANTHROPIC_BASE_URL=http://localhost:8085 in Claude Code config
        Set ANTHROPIC_API_KEY=bifrost-local (any non-empty string)
"""

import json
import logging
import time
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from shared.resolver import resolve

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bifrost.adapter")

app = FastAPI(title="BIFROST Anthropic Adapter", version="1.0.0")

ROUTER_URL = resolve("router_chat")

MODEL_ALIASES = {
    "claude-haiku-20240307":       "bifrost-auto",
    "claude-haiku-3-5":            "bifrost-auto",
    "claude-sonnet-20240229":      "bifrost-t2-general",
    "claude-sonnet-3-5":           "bifrost-t2-general",
    "claude-sonnet-3-7":           "bifrost-t2-general",
    "claude-opus-20240229":        "bifrost-forge-t1",
    "claude-opus-4":               "bifrost-forge-t1",
}


# ---------------------------------------------------------------------------
# Request translation helpers
# ---------------------------------------------------------------------------

def anthropic_to_openai_messages(messages: list[dict]) -> list[dict]:
    """Convert Anthropic message format to OpenAI/Router format."""
    out = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Anthropic content can be a string or list of content blocks
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        text_parts.append(str(block.get("content", "")))
                else:
                    text_parts.append(str(block))
            content = "\n".join(text_parts)

        out.append({"role": role, "content": content})
    return out


def openai_to_anthropic_response(result: dict, model: str) -> dict:
    """Convert OpenAI-format Router response to Anthropic wire format."""
    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    usage = result.get("usage", {})

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming translation
# ---------------------------------------------------------------------------

async def stream_anthropic(router_response, model: str, msg_id: str):
    """Convert Router SSE stream → Anthropic SSE stream format."""

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield sse("ping", {"type": "ping"})

    output_tokens = 0
    async for line in router_response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
            text = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if text:
                output_tokens += len(text.split())
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                })
        except json.JSONDecodeError:
            continue

    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def messages(request: Request):
    """Main Anthropic-compatible messages endpoint."""
    body = await request.json()

    model = body.get("model", "bifrost-local")
    messages_raw = body.get("messages", [])
    system = body.get("system", "")
    stream = body.get("stream", False)
    max_tokens = body.get("max_tokens", 4096)
    temperature = body.get("temperature", 0.7)

    oai_messages = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    oai_messages.extend(anthropic_to_openai_messages(messages_raw))

    # Map Anthropic model names to router aliases
    _req_model = body.get("model", "bifrost-auto")
    routed_model = MODEL_ALIASES.get(_req_model) or (
        _req_model if _req_model.startswith("bifrost-") else "bifrost-auto"
    )

    router_payload = {
        "model": routed_model,
        "messages": oai_messages,
        "stream": stream,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Forward complexity hint if present
    fwd_headers = {}
    hint = request.headers.get("X-Complexity-Hint")
    if hint:
        fwd_headers["X-Complexity-Hint"] = hint

    log.info(f"ADAPTER: model={model} stream={stream} messages={len(oai_messages)}")

    if stream:
        msg_id = f"msg_{uuid.uuid4().hex[:24]}"

        async def stream_gen():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", ROUTER_URL,
                    json=router_payload,
                    headers=fwd_headers,
                ) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        raise httpx.HTTPStatusError(err.decode()[:200], request=resp.request, response=resp)
                    async for chunk in stream_anthropic(resp, model, msg_id):
                        yield chunk

        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    else:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                ROUTER_URL, json=router_payload, headers=fwd_headers
            )
            resp.raise_for_status()
            result = resp.json()

        routed_model = result.get("model", model)
        log.info(f"ADAPTER: complete routed_to={routed_model}")
        return JSONResponse(openai_to_anthropic_response(result, routed_model))


@app.get("/health")
async def health():
    # Check Router is reachable
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            health_url = resolve("router_health") + "/router/status"
            r = await client.get(health_url)
            router_ok = r.status_code == 200
    except Exception:
        router_ok = False

    body = {
        "status": "ok" if router_ok else "degraded",
        "adapter": "bifrost-anthropic-adapter",
        "router": "ok" if router_ok else "unreachable",
        "router_url": ROUTER_URL,
    }
    return JSONResponse(content=body,
        status_code=200 if router_ok else 503)


@app.get("/")
async def root():
    return {
        "service": "BIFROST Anthropic Adapter",
        "version": "1.0.0",
        "endpoints": ["/v1/messages", "/health"],
        "router": ROUTER_URL,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "anthropic_adapter:app",
        host="0.0.0.0",
        port=8085,
        reload=False,
        log_level="info",
    )
