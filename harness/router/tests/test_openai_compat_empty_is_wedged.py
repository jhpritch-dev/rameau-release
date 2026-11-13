"""openai_compat must treat an empty assistant turn as wedged, in BOTH directions.

John 2026-10-05: "turn on for both directions".

THE ASYMMETRY THIS CLOSES. `ollama.py` passes empty_is_wedged=True on both of its CHAT paths
(:310, :312 streaming; :808 non-streaming) and deliberately NOT on the /api/generate FIM path,
where completing to nothing is legitimate. `openai_compat` passed it in NEITHER direction, so an
assistant turn with no content and no tool call -- a void, not an answer -- was refused on the
local chat tiers and accepted as a successful completion on every CLOUD tier. Which one a caller
got depended on nothing but which backend served the tier.

WHY IT IS SAFE HERE, unlike in ollama.py. This module has exactly ONE entry point,
openai_compat_chat_completion, and it always posts `messages` to `/chat/completions` (:148, :152).
There is no FIM/completion path through it, so there is no legitimate-empty shape to protect --
checked by AST before the flag was turned on.

WHAT IS ASSERTED, AND WHY IN PAIRS. Three cases must come out the OTHER way: a streamed tool
call, a non-streamed tool call, and a truncated empty stream. An implementation that refused every
empty response would fail all three; one that refused none fails the two protective cases.
"""
import asyncio
import json
import types

import httpx
import pytest

from backends import openai_compat as oc
from degenerate import WedgedBackendError

MSGS = [{"role": "user", "content": "hi"}]
USAGE = {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}
TOOL_CALL = [{"id": "c1", "type": "function",
              "function": {"name": "f", "arguments": "{}"}}]


# ---------------------------------------------------------------------------
# streaming helpers -- the shapes a provider actually emits
# ---------------------------------------------------------------------------

def _chunk(delta, finish=None, usage=None):
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk", "model": "m",
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return "data: %s\n\n" % json.dumps(obj)


EMPTY_STREAM = [_chunk({"role": "assistant"}), _chunk({"content": ""}),
                _chunk({}, finish="stop"), _chunk({}, finish="stop", usage=USAGE),
                "data: [DONE]\n\n"]

TOOL_STREAM = [_chunk({"role": "assistant"}), _chunk({"tool_calls": TOOL_CALL}),
               _chunk({}, finish="tool_calls"),
               _chunk({}, finish="tool_calls", usage=USAGE), "data: [DONE]\n\n"]

TRUNCATED_EMPTY_STREAM = [_chunk({"role": "assistant"}), _chunk({"content": ""}),
                          _chunk({}, finish="length"),
                          _chunk({}, finish="length", usage=USAGE), "data: [DONE]\n\n"]


def _fake_stream(chunks):
    async def fake(url, headers, payload, timeout):
        for c in chunks:
            yield c
    return fake


async def _drain(agen):
    out = []
    async for c in await agen if asyncio.iscoroutine(agen) else agen:
        out.append(c)
    return out


def _run_stream(monkeypatch, chunks):
    monkeypatch.setattr(oc, "_stream", _fake_stream(chunks))
    gen = oc.openai_compat_chat_completion(MSGS, provider="flm", stream=True)
    return asyncio.run(_drain(gen))


# ---------------------------------------------------------------------------
# non-streaming helper -- fake out httpx so no backend is contacted
# ---------------------------------------------------------------------------

def _install_fake_http(monkeypatch, body):
    class _Resp:
        status_code = 200

        def json(self):
            return body

        @property
        def text(self):
            return json.dumps(body)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(oc, "httpx", types.SimpleNamespace(
        AsyncClient=_Client, HTTPStatusError=httpx.HTTPStatusError))


def _resp(message, finish="stop"):
    return {"id": "chatcmpl-t", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": USAGE}


def _run_request(monkeypatch, body):
    _install_fake_http(monkeypatch, body)
    return asyncio.run(oc.openai_compat_chat_completion(MSGS, provider="flm", stream=False))


# ---------------------------------------------------------------------------
# the protective claims
# ---------------------------------------------------------------------------

def test_streamed_empty_assistant_turn_is_refused(monkeypatch):
    with pytest.raises(WedgedBackendError):
        _run_stream(monkeypatch, EMPTY_STREAM)


def test_nonstreamed_empty_assistant_turn_is_refused(monkeypatch):
    with pytest.raises(WedgedBackendError):
        _run_request(monkeypatch, _resp({"role": "assistant", "content": ""}))


# ---------------------------------------------------------------------------
# the pairs -- each must come out the OTHER way
# ---------------------------------------------------------------------------

def test_streamed_tool_call_with_empty_content_is_not_refused(monkeypatch):
    """A tool call carries no content BY DESIGN. Refusing it would break the thing the empty-body
    arm exists to protect."""
    out = _run_stream(monkeypatch, TOOL_STREAM)
    assert any("tool_calls" in c for c in out), out


def test_nonstreamed_tool_call_with_empty_content_is_not_refused(monkeypatch):
    got = _run_request(monkeypatch, _resp(
        {"role": "assistant", "content": "", "tool_calls": TOOL_CALL}, finish="tool_calls"))
    assert (got["choices"][0]["message"].get("tool_calls")), got


def test_streamed_empty_with_finish_length_is_delivered_not_refused(monkeypatch):
    """REGRESSION GUARD for degenerate.py:441-450 (2026-09-10): an empty body whose backend
    reported finish_reason=length is TRUNCATED, not wedged, and must still be delivered. Turning
    empty_is_wedged on must not undo that -- the truncation arm fires first."""
    out = _run_stream(monkeypatch, TRUNCATED_EMPTY_STREAM)
    assert any("length" in c for c in out), out
