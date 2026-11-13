"""Ceremony 4 Pattern 4: never run tool calls from a reply cut off at the output token limit.

A tool call whose JSON arguments were cut mid-string can parse into a shorter, different
argument (a truncated path or query) and run the wrong call silently. Pi refuses every call in
such a reply and asks the model to re-issue it (agent-loop.ts:227-232).

Two halves:
  * igor_tool_loop reads the finish reason and refuses the calls.
  * backends/ollama.py's non-stream /api/chat path stops hardcoding "stop", so an ollama
    tier can report the length stop at all (the streaming path already forwarded it).
"""
import asyncio
import json

import igor_tool_loop as itl


def _tool_name():
    names = sorted(n for n in itl.SAFE_TOOL_NAMES if n != "delegate_subtask")
    return "run_checks" if "run_checks" in names else names[0]


class Backend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"messages": list(kw["messages"]), "tools": kw.get("tools")})
        i = len(self.calls) - 1
        return self.responses[i] if i < len(self.responses) else _final("default final")


class Gateway:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, request_id):
        self.calls.append((name, arguments))
        return {"ok": True, "result": {"text": "ok"}}


def _tools_reply(finish_reason, n=1, **choice_extra):
    calls = [{"id": f"c{k}", "function": {"name": _tool_name(), "arguments": {"path": "src/ma"}}}
             for k in range(n)]
    choice = {"message": {"role": "assistant", "content": None, "tool_calls": calls},
              "finish_reason": finish_reason, **choice_extra}
    return {"choices": [choice]}


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _run(backend, gateway, max_iterations=12):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier="no-such-tier",
        messages=[{"role": "user", "content": "check the repo"}],
        temperature=0.0, max_tokens=None, caller_thinking=False,
        gateway_call_fn=gateway, max_iterations=max_iterations, total_turn_timeout_s=60.0,
        request_id="t-len"))


def test_length_stop_calls_are_not_executed():
    be, gw = Backend([_tools_reply("length", n=2), _final("done")]), Gateway()
    out = _run(be, gw)
    assert gw.calls == [], "a call from a cut-off reply must never reach the gateway"
    tool_msgs = [m for m in be.calls[1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c1"]
    for m in tool_msgs:
        body = json.loads(m["content"])
        assert body["kind"] == "length_stop" and "NOT executed" in body["error"]
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and tl["length_stops"] == 1


def test_the_reissued_call_runs():
    be, gw = Backend([_tools_reply("length"), _tools_reply("tool_calls"), _final("done")]), Gateway()
    out = _run(be, gw)
    assert len(gw.calls) == 1
    assert out["_bifrost_tool_loop"]["successful_tool_calls"] == 1
    assert out["_bifrost_tool_loop"]["length_stops"] == 1


def test_normal_stop_still_executes():
    """ollama's non-stream path reports "stop" for a completed tool reply; unchanged."""
    be, gw = Backend([_tools_reply("stop"), _final("done")]), Gateway()
    out = _run(be, gw)
    assert len(gw.calls) == 1 and out["_bifrost_tool_loop"]["length_stops"] == 0


def test_coalesce_rewrite_does_not_refuse_complete_calls():
    """reasoning_coalesce turns "stop" into "length" for an unclosed <think> block and keeps
    the backend's value. The backend said stop, so the arguments are whole: run them."""
    be = Backend([_tools_reply("length", _bifrost_original_finish_reason="stop"), _final("done")])
    gw = Gateway()
    out = _run(be, gw)
    assert len(gw.calls) == 1 and out["_bifrost_tool_loop"]["length_stops"] == 0


def test_only_length_stops_until_the_cap_says_so():
    """Without its own reason, N1 would report this as a tool-transport outage."""
    be, gw = Backend([_tools_reply("length")] * 3), Gateway()
    out = _run(be, gw, max_iterations=3)
    assert gw.calls == []
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "length_stop" and tl["length_stops"] == 3
    assert out["choices"][0]["finish_reason"] == "stop"
    assert "output token limit" in out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------- backends/ollama.py
def _ollama_chat(monkeypatch, data):
    import backends.ollama as ol

    async def fake_uncancellable(url, send):
        return data

    monkeypatch.setattr(ol, "npu_uncancellable", fake_uncancellable)
    return asyncio.run(ol._request_ollama(
        "http://127.0.0.1:9/v1/chat/completions",
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}))


def _data(done_reason, content="partial answer"):
    d = {"message": {"role": "assistant", "content": content}, "eval_count": 5,
         "eval_duration": 1_000_000_000, "prompt_eval_count": 3}
    if done_reason is not None:
        d["done_reason"] = done_reason
    return d


def test_ollama_nonstream_reports_a_length_stop(monkeypatch):
    r = _ollama_chat(monkeypatch, _data("length"))
    assert r["choices"][0]["finish_reason"] == "length"


def test_ollama_nonstream_other_done_reasons_stay_stop(monkeypatch):
    for dr in ("stop", None, "load", "unload"):
        r = _ollama_chat(monkeypatch, _data(dr))
        assert r["choices"][0]["finish_reason"] == "stop", dr


def test_ollama_length_with_unclosed_think_keeps_length(monkeypatch):
    """Coalesce only rewrites "stop"; a real length stop passes through untouched, with no
    _bifrost_original_finish_reason, so the loop guard sees the backend's own value."""
    r = _ollama_chat(monkeypatch, _data("length", content="<think>still reasoning"))
    c = r["choices"][0]
    assert c["finish_reason"] == "length" and "_bifrost_original_finish_reason" not in c
