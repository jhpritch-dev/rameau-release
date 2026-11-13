"""Non-stream /v1 responses built from Ollama native /api/chat must carry OpenAI-shaped tool_calls.

Measured 2026-09-11 on the live Router (v439), model bifrost-igor-solo, stream:false, one client tool:
tool_calls came back in Ollama's NATIVE shape -- `function.arguments` a JSON object, `index` inside
`function`, no `type`. The streaming path and Ollama's own /v1 were spec-correct. The OpenAI spec
requires `arguments` as a JSON-encoded STRING and `type: "function"`; anthropic_compat.py:198 calls
json.loads(arguments) and raises TypeError on a dict.

Offline: backends.ollama's httpx.AsyncClient is replaced by a fake that returns a native body.
"""
import asyncio
import json

import anthropic_compat
import backends.ollama as bo

_URL = "http://127.0.0.1:9/v1/chat/completions"
_PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "read it"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}


def _native(message, done_reason="stop"):
    return {"model": "m", "message": message, "done": True, "done_reason": done_reason,
            "eval_count": 5, "eval_duration": 1_000_000_000, "prompt_eval_count": 3}


def _run(monkeypatch, message, done_reason="stop"):
    sent = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return _native(message, done_reason)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.append((url, json))
            return _Resp()

    monkeypatch.setattr(bo.httpx, "AsyncClient", _Client)
    out = asyncio.run(bo._request_ollama(_URL, dict(_PAYLOAD)))
    assert sent and sent[0][0].endswith("/api/chat")
    return out


_TWO_CALLS = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_x1", "function": {"index": 0, "name": "read_file", "arguments": {"path": "scripts/a.py"}}},
    {"function": {"index": 1, "name": "list_dir", "arguments": {}}},
]}


def test_arguments_are_json_strings_and_type_is_function(monkeypatch):
    msg = _run(monkeypatch, _TWO_CALLS)["choices"][0]["message"]
    tcs = msg["tool_calls"]
    assert len(tcs) == 2
    for tc in tcs:
        assert tc["type"] == "function"
        assert isinstance(tc["function"]["arguments"], str)
        assert "index" not in tc["function"]
    assert json.loads(tcs[0]["function"]["arguments"]) == {"path": "scripts/a.py"}
    assert json.loads(tcs[1]["function"]["arguments"]) == {}
    assert tcs[0]["function"]["name"] == "read_file"


def test_ids_are_kept_or_generated(monkeypatch):
    tcs = _run(monkeypatch, _TWO_CALLS)["choices"][0]["message"]["tool_calls"]
    assert tcs[0]["id"] == "call_x1"
    assert isinstance(tcs[1]["id"], str) and tcs[1]["id"].startswith("call_")


def test_string_arguments_pass_through_unchanged(monkeypatch):
    raw = '{"path": "x.py"}'
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_s", "type": "function", "function": {"name": "read_file", "arguments": raw}}]}
    tc = _run(monkeypatch, msg)["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["arguments"] == raw


def test_no_tool_calls_leaves_message_without_the_key(monkeypatch):
    msg = _run(monkeypatch, {"role": "assistant", "content": "plain answer"})["choices"][0]["message"]
    assert "tool_calls" not in msg
    assert msg["content"] == "plain answer"


def test_anthropic_surface_converts_the_router_response(monkeypatch):
    """The consumer that broke: openai_to_anthropic_response json.loads()es arguments."""
    resp = _run(monkeypatch, _TWO_CALLS)
    blocks = anthropic_compat.openai_to_anthropic_response(resp)["content"]
    uses = [b for b in blocks if b.get("type") == "tool_use"]
    assert [u["input"] for u in uses] == [{"path": "scripts/a.py"}, {}]


# finish_reason (John 2026-09-12: "tool_calls", per the OpenAI spec). "length" keeps precedence:
# igor_tool_loop's P4 guard refuses tool calls from a reply cut off at num_predict.

def test_finish_reason_is_tool_calls_when_the_reply_has_tool_calls(monkeypatch):
    assert _run(monkeypatch, _TWO_CALLS)["choices"][0]["finish_reason"] == "tool_calls"


def test_finish_reason_stays_stop_without_tool_calls(monkeypatch):
    msg = {"role": "assistant", "content": "plain answer"}
    assert _run(monkeypatch, msg)["choices"][0]["finish_reason"] == "stop"


def test_length_wins_over_tool_calls(monkeypatch):
    out = _run(monkeypatch, _TWO_CALLS, done_reason="length")
    assert out["choices"][0]["finish_reason"] == "length"
    assert len(out["choices"][0]["message"]["tool_calls"]) == 2
