"""Ceremony 4 Pattern 1: the tool loop must keep its prompt inside the tier's context window.

Before: the only context check ran once, before the loop, without the loop's tool definitions.
The loop then re-sent a growing message list up to 12 times, and a single 32 KB tool result
(~8k tokens) could fill a whole 8k window; ollama silently front-truncates when that happens.

The first two tests use a REAL 8k tier from fleet_config, so they fail on the pre-change loop
(which never looked at num_ctx) and pass on the new one.
"""
import asyncio
import itertools
import json

import pytest

import igor_tool_loop as itl
from context_overflow import estimate_prompt_tokens


def _tool_name():
    names = sorted(n for n in itl.SAFE_TOOL_NAMES if n != "delegate_subtask")
    return "run_checks" if "run_checks" in names else names[0]


def _tier_with_ctx(ctx):
    from config import TIER_BACKENDS
    for tier in TIER_BACKENDS:
        b = TIER_BACKENDS.get(tier) or {}
        if int(b.get("num_ctx") or 0) == ctx and b.get("type") == "ollama":
            return tier
    pytest.skip(f"no ollama tier with num_ctx={ctx} in fleet_config")


class Backend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"messages": list(kw["messages"]), "tools": kw.get("tools")})
        i = len(self.calls) - 1
        return self.responses[i] if i < len(self.responses) else _final("default final")


class Gateway:
    def __init__(self, text_len):
        self.text = "x" * text_len
        self.calls = 0

    async def __call__(self, name, arguments, request_id):
        self.calls += 1
        return {"ok": True, "result": {"text": self.text}}


_SEQ = itertools.count()


def _tools_reply(n=1):
    # Unique arguments on every call, so no test here depends on repeated identical calls.
    calls = [{"id": f"c{k}", "function": {"name": _tool_name(), "arguments": {"seq": next(_SEQ)}}}
             for k in range(n)]
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls},
                         "finish_reason": "tool_calls"}]}


def _final(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _run(backend, gateway, tier, messages=None, max_iterations=12):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier=tier,
        messages=messages or [{"role": "user", "content": "check the repo"}],
        temperature=0.0, max_tokens=None, caller_thinking=False,
        gateway_call_fn=gateway, max_iterations=max_iterations, total_turn_timeout_s=60.0,
        request_id="t-ctx"))


def test_result_cap_follows_the_real_8k_tier():
    """A 20,000-char tool result reaches the model capped at 5,734 chars on an 8k tier
    (it went through whole before: 20,000 < 32 KB)."""
    tier = _tier_with_ctx(8192)
    be = Backend([_tools_reply(1), _final("done")])
    out = _run(be, Gateway(20000), tier)
    tool_msgs = [m for m in be.calls[1]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    c = tool_msgs[0]["content"]
    # Ceremony 4 P3: over the cap the result is PAGED (note + head + tail, within the cap).
    assert len(c) <= 5734, len(c)
    assert "tool_output_page(id='r0_0'" in c
    assert json.dumps({"text": "x" * 20000})[:512] in c
    assert out["_bifrost_tool_loop"]["reason"] == "completed"


def test_latest_block_over_the_window_stops_with_context_budget():
    """Six results in one turn (~8.6k tokens even after the cap) cannot fit an 8k window.
    Nothing older exists to prune or summarize, so the loop must stop -- not re-send it."""
    tier = _tier_with_ctx(8192)
    be = Backend([_tools_reply(6), _final("should never be reached")])
    out = _run(be, Gateway(20000), tier)
    assert len(be.calls) == 1, "the over-window prompt must not be sent"
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "context_budget"
    assert tl["ctx_len"] == 8192
    assert out["choices"][0]["finish_reason"] == "stop"
    assert "context window" in out["choices"][0]["message"]["content"]


def test_result_cap_values():
    assert itl._result_cap_chars(None) == 32 * 1024
    assert itl._result_cap_chars(4096) == 2867
    assert itl._result_cap_chars(8192) == 5734
    assert itl._result_cap_chars(16384) == 11468
    assert itl._result_cap_chars(65536) == 32 * 1024
    assert itl._result_cap_chars(1000) == 2048  # floor


def test_unknown_ctx_is_todays_behaviour(monkeypatch):
    monkeypatch.setattr(itl, "_tier_num_ctx", lambda tier: None)
    be = Backend([_tools_reply(1) for _ in range(6)] + [_final("done")])
    out = _run(be, Gateway(20000), "no-such-tier")
    assert all("[truncated " not in m.get("content", "") for c in be.calls for m in c["messages"]
               if m.get("role") == "tool")
    assert all(c["tools"] for c in be.calls), "no summary call without a known window"
    assert out["_bifrost_tool_loop"]["reason"] == "completed"
    assert out["_bifrost_tool_loop"]["compactions"] == 0


def test_first_call_is_never_blocked(monkeypatch):
    """Iteration 0 is the inbound pre-flight's job; the loop does not second-guess it."""
    monkeypatch.setattr(itl, "_tier_num_ctx", lambda tier: 8192)
    be = Backend([_final("answered")])
    out = _run(be, Gateway(10), "t", messages=[{"role": "user", "content": "y" * 200000}])
    assert len(be.calls) == 1 and out["_bifrost_tool_loop"]["reason"] == "completed"


def _tiny_tools(monkeypatch):
    name = _tool_name()
    monkeypatch.setattr(itl, "get_safe_tool_definitions",
                        lambda role="igor": [{"type": "function", "function": {"name": name, "parameters": {}}}])


def test_prune_keeps_the_latest_block_and_avoids_a_summary(monkeypatch):
    _tiny_tools(monkeypatch)
    monkeypatch.setattr(itl, "_tier_num_ctx", lambda tier: 40000)  # cap 28,000 chars; budget 38,976
    be = Backend([_tools_reply(1) for _ in range(5)] + [_final("done")])
    out = _run(be, Gateway(27000), "t")
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and tl["compactions"] == 0 and tl["pruned_results"] >= 1
    last = be.calls[-1]["messages"]
    tool_msgs = [m for m in last if m.get("role") == "tool"]
    assert itl._PRUNE_MARKER not in tool_msgs[-1]["content"], "the latest block stays verbatim"
    assert any(itl._PRUNE_MARKER in m["content"] for m in tool_msgs[:-1])
    for c in be.calls:
        assert estimate_prompt_tokens(c["messages"], c["tools"]) <= 38976


def test_summary_replaces_older_blocks_once(monkeypatch):
    _tiny_tools(monkeypatch)
    monkeypatch.setattr(itl, "_tier_num_ctx", lambda tier: 40000)
    monkeypatch.setattr(itl, "_PRUNE_HEAD", 26000)  # make pruning too weak, forcing the summary
    be = Backend([_tools_reply(1) for _ in range(5)] + [_final("SUMMARY: facts so far"), _final("done")])
    out = _run(be, Gateway(27000), "t")
    summary_call = be.calls[5]
    assert summary_call["tools"] is None
    assert summary_call["messages"][-1]["content"] == itl._COMPACT_INSTRUCTION
    after = be.calls[6]["messages"]
    notes = [m for m in after if m.get("role") == "user" and m["content"].startswith(itl._COMPACT_NOTE_PREFIX)]
    assert len(notes) == 1 and "SUMMARY: facts so far" in notes[0]["content"]
    assert after.index(notes[0]) > next(i for i, m in enumerate(after) if m["content"] == "check the repo"), \
        "the summary goes after the task, never in front of it"
    assert sum(1 for m in after if m.get("role") == "tool") == 1, "only the latest block survives verbatim"
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and tl["compactions"] == 1


def test_a_failed_summary_leaves_messages_intact(monkeypatch):
    _tiny_tools(monkeypatch)
    monkeypatch.setattr(itl, "_tier_num_ctx", lambda tier: 40000)
    monkeypatch.setattr(itl, "_PRUNE_HEAD", 26000)
    be = Backend([_tools_reply(1) for _ in range(5)] + [_final("   "), _final("done")])  # empty summary
    out = _run(be, Gateway(27000), "t")
    assert not any(m.get("content", "").startswith(itl._COMPACT_NOTE_PREFIX)
                   for c in be.calls for m in c["messages"] if isinstance(m.get("content"), str))
    assert out["_bifrost_tool_loop"]["compactions"] == 1  # attempted once, never retried
