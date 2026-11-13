"""
backends/n8n.py -- n8n workflow backend for BIFROST Router

Dispatches a subtask to an n8n webhook endpoint and awaits the response.
Uses n8n's "Respond to Webhook" node pattern for synchronous completion.

fleet_config.json entry:
  {
    "tier": "n8n-ops",
    "type": "n8n_workflow",
    "base_url": "http://192.168.x.4:5678/webhook/{workflow-id}",
    "active_model": "n8n",
    ...
  }
"""

import httpx
import logging

log = logging.getLogger("bifrost.backends.n8n")

_DEFAULT_TIMEOUT = 60.0   # n8n workflows should complete well within 60s


async def n8n_workflow_chat_completion(
    messages: list[dict],
    base_url: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """
    POST the last user message to an n8n webhook and return a
    chat-completion-shaped response dict.

    n8n workflow must use a "Respond to Webhook" node that returns:
      {"output": "<result text>"}
    or any JSON -- the full response is stringified as output if "output"
    key is not present.
    """
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            prompt = msg.get("content", "")
            break

    payload = {
        "prompt": prompt,
        "messages": messages,
    }

    log.info(f"n8n dispatch: POST {base_url} prompt={prompt[:80]!r}")

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        log.error(f"n8n timeout after {timeout}s: {base_url}")
        raise
    except httpx.HTTPStatusError as e:
        log.error(f"n8n HTTP error {e.response.status_code}: {base_url}")
        raise
    except Exception as e:
        log.error(f"n8n error: {e}")
        raise

    if isinstance(data, dict):
        output = (
            data.get("output")
            or data.get("result")
            or data.get("text")
            or data.get("message")
            or str(data)
        )
    elif isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            output = first.get("output") or first.get("json", {}).get("output") or str(first)
        else:
            output = str(first)
    else:
        output = str(data)

    log.info(f"n8n response: {str(output)[:120]!r}")

    return {
        "choices": [{"message": {"content": output, "role": "assistant"}}],
        "usage": {"total_tokens": 0},
        "model": "n8n",
    }
