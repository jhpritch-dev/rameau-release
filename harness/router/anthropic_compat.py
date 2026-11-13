"""
anthropic_compat.py — Anthropic Messages API <-> OpenAI Chat Completions translation

Allows Claude Code and Claude CoWork to route through BIFROST Router
by translating between Anthropic's /v1/messages format and the Router's
internal OpenAI-compatible dispatch layer.

Translation map:
  Anthropic request  -> OpenAI messages
  Anthropic tools    -> OpenAI tools (input_schema -> parameters)
  OpenAI response    -> Anthropic response (choices -> content[])
  OpenAI tool_calls  -> Anthropic tool_use content blocks
  SSE chunks         -> Anthropic streaming event format
"""

import json
import logging
import uuid

logger = logging.getLogger("bifrost.anthropic_compat")




# ---------------------------------------------------------------------------
# Model alias mapping: Anthropic-protocol model names -> local Router aliases.
# Added 2026-05-07 v101 to make Claude Code / Continue.dev work with the
# bifrost-claude-* launcher family without per-client model configuration.
# Pass-through for unmapped names so future Anthropic releases don't 500.
# ---------------------------------------------------------------------------

ANTHROPIC_TO_LOCAL = {
    "claude-opus-4-7":   "bifrost-igor",   # Tier 2.5 Forge = qwen3-coder:30b (real Igor = bifrost-igor-solo)
    "claude-opus-4-7[1m]":   "bifrost-igor",   # 1M context variant (Claude Code default)
    "claude-sonnet-4-6[1m]": "bifrost-igor",   # 1M context variant
    "claude-haiku-4-5[1m]":  "phi4:14b",       # 1M context fast tier
    "claude-sonnet-4-6": "bifrost-igor",   # currently same; update when T2 fills
    "claude-haiku-4-5":  "phi4:14b",       # fast tier
    # bare-version variants (some clients drop the trailing version digit)
    "claude-opus-4":     "bifrost-igor",
    "claude-sonnet-4":   "bifrost-igor",
    "claude-haiku-4":    "phi4:14b",
    "claude-opus":       "bifrost-igor",
    "claude-sonnet":     "bifrost-igor",
    "claude-haiku":      "phi4:14b",
}


def _map_anthropic_model(name: str | None) -> str:
    """Resolve an Anthropic-protocol model name to a local Router alias.

    Pass through unrecognized names so future Anthropic model releases do not
    break the route -- the upstream tier dispatcher will then either find the
    name as a Router alias or fall back via existing default behavior.
    """
    if not name:
        return "bifrost-igor"
    return ANTHROPIC_TO_LOCAL.get(name, name)


# ---------------------------------------------------------------------------
# Request translation: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

def anthropic_to_openai_messages(
    anthropic_messages: list[dict],
    system: str | None = None,
) -> list[dict]:
    """
    Convert Anthropic messages array to OpenAI messages array.

    Key differences:
    - Anthropic: system is a top-level string field
    - OpenAI: system is messages[0] with role="system"
    - Anthropic: content can be a string or list of content blocks
    - OpenAI: content is a string (we flatten content blocks)
    """
    openai_messages = []

    if system:
        openai_messages.append({"role": "system", "content": system})

    for msg in anthropic_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        tool_content = block.get("content", "")
                        if isinstance(tool_content, list):
                            tool_content = " ".join(
                                b.get("text", "") for b in tool_content
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        text_parts.append(f"[Tool result: {tool_content}]")
                    elif block.get("type") == "tool_use":
                        pass
                    else:
                        text_parts.append(str(block))
                else:
                    text_parts.append(str(block))
            content = "\n".join(text_parts)

        if role == "assistant":
            openai_messages.append({"role": "assistant", "content": content})
        else:
            openai_messages.append({"role": "user", "content": content})

    return openai_messages


def anthropic_to_openai_tools(anthropic_tools: list[dict] | None) -> list[dict] | None:
    """
    Convert Anthropic tool definitions to OpenAI tool definitions.

    Anthropic: {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI:    {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    if not anthropic_tools:
        return None

    openai_tools = []
    for tool in anthropic_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return openai_tools


def anthropic_request_to_openai(body: dict) -> dict:
    """
    Translate a full Anthropic /v1/messages request body to
    OpenAI /v1/chat/completions format.
    """
    system = body.get("system")
    if isinstance(system, list):
        system = " ".join(
            block.get("text", "") for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )

    openai_body = {
        "model": _map_anthropic_model(body.get("model", "bifrost-t2-gemma4")),
        "messages": anthropic_to_openai_messages(
            body.get("messages", []),
            system=system,
        ),
        "stream": body.get("stream", False),
        "max_tokens": body.get("max_tokens", 4096),
    }

    if body.get("temperature") is not None:
        openai_body["temperature"] = body["temperature"]

    tools = anthropic_to_openai_tools(body.get("tools"))
    if tools:
        openai_body["tools"] = tools

    return openai_body


# ---------------------------------------------------------------------------
# Response translation: OpenAI -> Anthropic
# ---------------------------------------------------------------------------

def openai_to_anthropic_response(openai_response: dict, model: str = "bifrost") -> dict:
    """
    Translate an OpenAI /v1/chat/completions response to
    Anthropic /v1/messages response format.
    """
    choice = {}
    if openai_response.get("choices"):
        choice = openai_response["choices"][0]

    message = choice.get("message", {})
    content_text = message.get("content") or ""
    finish_reason = choice.get("finish_reason", "end_turn")

    content_blocks = []

    if content_text:
        content_blocks.append({"type": "text", "text": content_text})

    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            tool_input = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {"raw": fn.get("arguments", "")}

        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:16]}"),
            "name": fn.get("name", ""),
            "input": tool_input,
        })

    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")
    if tool_calls:
        stop_reason = "tool_use"

    usage = openai_response.get("usage", {})

    return {
        "id": openai_response.get("id", f"msg_{uuid.uuid4().hex[:16]}"),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------

def make_anthropic_stream_events(model: str, message_id: str):
    """Return the opening Anthropic streaming events."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
    ]


def openai_chunk_to_anthropic_events(chunk_data: dict) -> list[dict]:
    """
    Translate one OpenAI SSE chunk to zero or more Anthropic SSE events.
    """
    events = []
    choices = chunk_data.get("choices", [])
    if not choices:
        return events

    delta = choices[0].get("delta", {})
    finish_reason = choices[0].get("finish_reason")

    text = delta.get("content")
    if text:
        events.append({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        })

    if finish_reason:
        stop_reason_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
        }
        stop_reason = stop_reason_map.get(finish_reason, "end_turn")
        events.append({"type": "content_block_stop", "index": 0})
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 0},
        })
        events.append({"type": "message_stop"})

    return events


def format_anthropic_sse(event: dict) -> str:
    """Format a dict as an Anthropic SSE event string."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
