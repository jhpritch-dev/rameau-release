"""Pattern 2 (2026-09-11): tool-loop backend-error recovery, opt-in escalation, leaf give-up.

Before Pattern 2 any exception from the backend ended the loop at once with a 200 apology
(reason backend_error), which main.py's cascade reads as a success, so it never tried the next
tier. Now the loop classifies the failure, recovers on the same tier where nothing below already
retried (budget 2 per loop), and -- only when the caller opts in -- raises ToolLoopBackendError so
the cascade hops. A delegated leaf that gives up is reported ok:False instead of handing its
apology to the parent as a successful result.
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import degenerate
import igor_tool_loop as itl

ROOT = Path(__file__).resolve().parents[1]
_REQ = httpx.Request("POST", "http://backend.invalid/v1/chat/completions")


def _status(code, text=""):
    return httpx.HTTPStatusError(f"HTTP {code}", request=_REQ,
                                 response=httpx.Response(code, request=_REQ, text=text))


def _tool_name():
    names = sorted(n for n in itl.SAFE_TOOL_NAMES if n != "delegate_subtask")
    return "run_checks" if "run_checks" in names else names[0]


def _tool_reply(seq):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{seq}", "function": {"name": _tool_name(), "arguments": {"seq": seq}}}]},
        "finish_reason": "tool_calls"}]}


def _final(text="done"):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


class Backend:
    """Scripted backend: each outcome is a response dict, or an exception to raise."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"tier": kw["tier"], "tools": kw.get("tools"), "messages": list(kw["messages"])})
        i = len(self.calls) - 1
        o = self.outcomes[i] if i < len(self.outcomes) else _final("default final")
        if isinstance(o, BaseException):
            raise o
        return o


class TierBackend:
    """Routes each call to a per-tier Backend (parent loop vs delegated leaf loop)."""

    def __init__(self, by_tier):
        self.by_tier = by_tier

    async def __call__(self, **kw):
        return await self.by_tier[kw["tier"]](**kw)


class Gateway:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, arguments, request_id):
        self.calls.append((name, arguments))
        return {"ok": True, "result": {"text": "ok"}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(itl, "_RECOVERY_PAUSE_S", 0.0, raising=False)
    monkeypatch.setattr(itl, "_VIOLATION_LOG", str(tmp_path / "violations.jsonl"))
    monkeypatch.setattr(itl, "_CANARY_EVENTS_LOG", str(tmp_path / "canary.jsonl"))


def _typed(monkeypatch, backend_type):
    monkeypatch.setattr(itl, "_backend_type", lambda tier: backend_type, raising=False)


def _run(backend, *, gateway=None, raise_on=None, tier="no-such-tier", max_iterations=8,
         timeout=60.0, resolver=None):
    kw = dict(backend_dispatch_fn=backend, tier=tier,
              messages=[{"role": "user", "content": "check the repo"}],
              temperature=0.0, max_tokens=None, caller_thinking=False,
              gateway_call_fn=gateway or Gateway(), max_iterations=max_iterations,
              total_turn_timeout_s=timeout, request_id="t-rec", tier_resolver=resolver)
    if raise_on is not None:
        kw["raise_on_backend_error"] = raise_on
    return asyncio.run(itl.run_tool_loop(**kw))


def _content(out):
    return out["choices"][0]["message"]["content"]


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectError("refused"), "TRANSPORT"),
    (httpx.ReadTimeout("slow"), "TIMEOUT"),
    (_status(500), "SERVER"),
    (_status(503), "SERVER"),
    (_status(429), "RATE_LIMIT"),
    (_status(400, "This model's maximum context length is 8192 tokens"), "CONTEXT"),
    (_status(413), "CONTEXT"),
    (_status(400, "invalid tool schema"), "BAD_REQUEST"),
    (_status(404), "BAD_REQUEST"),
    (degenerate.WedgedBackendError("ollama chat: empty reply"), "WEDGED"),
    (RuntimeError("prompt is too long for the context window"), "CONTEXT"),
    (RuntimeError("boom"), "OTHER"),
])
def test_classify(exc, code):
    assert itl._classify_backend_error(exc) == code


# ------------------------------------------------------------------ same-tier recovery
@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectError("refused"), "TRANSPORT"),
    (httpx.ReadTimeout("slow"), "TIMEOUT"),
    (_status(500), "SERVER"),
    (_status(502), "SERVER"),
    (degenerate.WedgedBackendError("ollama chat: empty reply"), "WEDGED"),
])
def test_ollama_tier_recovers_on_the_same_tier(monkeypatch, exc, code):
    _typed(monkeypatch, "ollama")
    be = Backend([exc, _final("ok")])
    out = _run(be)
    assert len(be.calls) == 2
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and _content(out) == "ok"
    assert tl["recoveries"] == 1 and tl["error_code"] == code


@pytest.mark.parametrize("backend_type", ["openai_compat", "anthropic"])
@pytest.mark.parametrize("exc,calls", [
    (_status(500), 2),
    (_status(502), 2),
    (_status(503), 1),                  # backends/_retry.py already retried these 4x
    (_status(504), 1),
    (_status(429), 1),
    (httpx.ReadTimeout("slow"), 1),
    (httpx.ConnectError("refused"), 1),
])
def test_cloud_tier_retries_only_what_retry_py_does_not(monkeypatch, backend_type, exc, calls):
    _typed(monkeypatch, backend_type)
    be = Backend([exc, _final("ok")])
    out = _run(be)
    assert len(be.calls) == calls
    assert out["_bifrost_tool_loop"]["reason"] == ("completed" if calls == 2 else "backend_error")


def test_unknown_tier_type_keeps_the_old_behaviour(monkeypatch):
    # Regression guard (passes before Pattern 2 too): no type -> one call, the old terminal.
    _typed(monkeypatch, None)
    be = Backend([httpx.ConnectError("refused"), _final("never reached")])
    out = _run(be)
    assert len(be.calls) == 1
    assert out["_bifrost_tool_loop"]["reason"] == "backend_error"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_bad_request_is_not_retried(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([_status(400, "invalid tool schema"), _final("never reached")])
    out = _run(be)
    assert len(be.calls) == 1
    assert out["_bifrost_tool_loop"]["error_code"] == "BAD_REQUEST"


def test_budget_is_two_per_loop(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([httpx.ConnectError("x")] * 4)
    out = _run(be)
    assert len(be.calls) == 3
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "backend_error" and tl["recoveries"] == 2


def test_budget_spans_iterations_not_per_iteration(monkeypatch):
    _typed(monkeypatch, "ollama")
    gw = Gateway()
    be = Backend([httpx.ConnectError("x"), _tool_reply(0),
                  httpx.ConnectError("x"), _tool_reply(1),
                  httpx.ConnectError("x"), _final("never reached")])
    out = _run(be, gateway=gw)
    assert len(be.calls) == 5 and len(gw.calls) == 2
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "backend_error" and tl["recoveries"] == 2


def test_a_retry_never_outlives_the_turn_budget(monkeypatch):
    _typed(monkeypatch, "ollama")
    monkeypatch.setattr(itl, "_RECOVERY_PAUSE_S", 5.0, raising=False)
    be = Backend([httpx.ConnectError("x"), _final("never reached")])
    out = _run(be, timeout=1.0)
    assert len(be.calls) == 1
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "backend_error" and tl["recoveries"] == 0 and tl["error_code"] == "TRANSPORT"


# ------------------------------------------------------------------ context overflow
def test_context_error_compacts_then_retries(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([_tool_reply(0), _tool_reply(1),
                  _status(400, "maximum context length exceeded"),
                  _final("summary of the tool work"),          # the compaction call (no tools)
                  _final("answer")])
    out = _run(be)
    assert len(be.calls) == 5
    assert be.calls[3]["tools"] is None, "the recovery must summarize with no tools"
    tl = out["_bifrost_tool_loop"]
    assert tl["reason"] == "completed" and _content(out) == "answer"
    assert tl["error_code"] == "CONTEXT" and tl["recoveries"] == 1 and tl["compactions"] == 1


def test_context_error_with_nothing_to_compact_escalates(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([_status(400, "maximum context length exceeded")])
    with pytest.raises(itl.ToolLoopBackendError) as ei:
        _run(be, raise_on=True)
    assert ei.value.code == "CONTEXT" and len(be.calls) == 1


# ------------------------------------------------------------------ opt-in raise
def test_opt_in_raises_a_typed_error(monkeypatch):
    _typed(monkeypatch, None)
    be = Backend([RuntimeError("boom")])
    with pytest.raises(itl.ToolLoopBackendError) as ei:
        _run(be, raise_on=True)
    assert isinstance(ei.value, RuntimeError)       # main.py's `except Exception` catches it
    assert ei.value.code == "OTHER" and ei.value.recoveries == 0
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_opt_in_raises_after_the_budget_is_spent(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([httpx.ConnectError("x")] * 3)
    with pytest.raises(itl.ToolLoopBackendError) as ei:
        _run(be, raise_on=True)
    assert ei.value.code == "TRANSPORT" and ei.value.recoveries == 2 and len(be.calls) == 3


def test_default_does_not_raise(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([httpx.ConnectError("x")] * 3)
    out = _run(be)
    assert len(be.calls) == 3
    assert out["_bifrost_tool_loop"]["reason"] == "backend_error"
    assert out["choices"][0]["finish_reason"] == "stop"


def test_recovered_failure_does_not_raise(monkeypatch):
    _typed(monkeypatch, "ollama")
    be = Backend([httpx.ConnectError("x"), _final("ok")])
    out = _run(be, raise_on=True)
    assert out["_bifrost_tool_loop"]["reason"] == "completed"


def test_no_failure_reports_no_error_code(monkeypatch):
    _typed(monkeypatch, "ollama")
    out = _run(Backend([_final("ok")]))
    tl = out["_bifrost_tool_loop"]
    assert tl["error_code"] is None and tl["recoveries"] == 0


# ------------------------------------------------------------------ leaf give-up
def _delegate_reply():
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "d0", "function": {"name": "delegate_subtask", "arguments": {
            "subtask": "look it up", "leaf_alias": "bifrost-granite", "mode": "tools"}}}]},
        "finish_reason": "tool_calls"}]}


def _leaf_run(monkeypatch, leaf_outcomes):
    _typed(monkeypatch, None)
    parent = Backend([_delegate_reply(), _final("parent done")])
    leaf = Backend(leaf_outcomes)
    out = _run(TierBackend({"parent-tier": parent, "leaf-tier": leaf}), tier="parent-tier",
               resolver={"bifrost-granite": "leaf-tier"}.get)
    tool_msgs = [m for m in parent.calls[1]["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "d0"]
    assert len(tool_msgs) == 1
    return out, json.loads(tool_msgs[0]["content"]), leaf


def test_leaf_that_gave_up_is_not_a_successful_result(monkeypatch):
    out, body, leaf = _leaf_run(monkeypatch, [RuntimeError("leaf backend down")])
    assert len(leaf.calls) == 1
    assert body.get("kind") == "leaf_gave_up", body
    assert "reason=backend_error" in body["error"]
    assert "answer" not in body, "the leaf's apology must not reach the parent as an answer"
    tl = out["_bifrost_tool_loop"]
    assert tl["successful_tool_calls"] == 0 and "delegate_subtask" not in tl["tools_called"]


def test_leaf_that_answered_is_still_a_success(monkeypatch):
    # Regression guard (passes before Pattern 2 too).
    out, body, _ = _leaf_run(monkeypatch, [_final("leaf answer")])
    assert body["answer"] == "leaf answer"
    assert out["_bifrost_tool_loop"]["successful_tool_calls"] == 1


def test_breach_inside_a_leaf_halts_the_parent_turn(monkeypatch):
    # Force a breach in the leaf only: the leaf's gate lets "evil_tool" through, its breach canary
    # does not allow it. Before the fix the parent's gather turned the leaf's ToolGateBreach into a
    # dispatch_error and the parent kept going (another model call, a normal answer).
    _typed(monkeypatch, None)
    real_safe, real_exec = itl._safe_for_role, itl._executable_for_role
    monkeypatch.setattr(itl, "_safe_for_role",
                        lambda role: (frozenset(real_safe(role)) | {"evil_tool"}) if role == "leaf" else real_safe(role))
    monkeypatch.setattr(itl, "_executable_for_role", lambda role: set(real_exec(role)) - {"evil_tool"})
    alerts = []
    monkeypatch.setattr(itl, "_ha_notify_breach", lambda tool, rid: alerts.append(tool))
    evil = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "e0", "function": {"name": "evil_tool", "arguments": {}}}]}, "finish_reason": "tool_calls"}]}
    parent = Backend([_delegate_reply(), _final("parent done")])
    leaf = Backend([evil, _final("leaf done")])
    with pytest.raises(itl.ToolGateBreach):
        _run(TierBackend({"parent-tier": parent, "leaf-tier": leaf}), tier="parent-tier",
             resolver={"bifrost-granite": "leaf-tier"}.get)
    assert alerts == ["evil_tool"], "the leaf's canary must still fire exactly once"
    assert len(parent.calls) == 1, "the parent must make no model call after the breach"


# ------------------------------------------------------------------ who opts in
def _call_block(src, opener):
    i = src.index(opener)
    return src[i:src.index("\n                )", i)]


def test_main_cascade_opts_in():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "raise_on_backend_error=True" in _call_block(src, "result = await run_tool_loop(")


def test_main_halts_a_breach_instead_of_escalating():
    import main
    r = main._tool_gate_breach_response(itl.ToolGateBreach("x"), True, "some-tier", "rid")
    assert r is not None and r.status_code == 500
    body = json.loads(r.body)
    assert body["error"]["code"] == "TOOL_GATE_BREACH" and body["error"]["type"] == "bifrost_tool_gate_breach"
    # Every other failure still escalates (None = fall through to the cascade's catch-all).
    assert main._tool_gate_breach_response(RuntimeError("x"), True, "some-tier", "rid") is None
    assert main._tool_gate_breach_response(
        itl.ToolLoopBackendError("OTHER", "some-tier", 0, RuntimeError("x")), True, "some-tier", "rid") is None
    assert main._tool_gate_breach_response(RuntimeError("x"), False, "some-tier", "rid") is None


def test_main_cascade_checks_for_a_breach_before_escalating():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("result = await run_tool_loop(")
    j = src.index("except Exception as e:", i)
    block = src[j:src.index("last_error = e", j)]
    assert "_tool_gate_breach_response(e, _used_loop, tier, _request_id)" in block
    assert "return _breach_resp" in block


def test_subtask_graph_keeps_the_default():
    # Its give-up branch falls back to a plain _call_tier (test_leg_tool_loop_fallback); it must
    # keep receiving the backend_error terminal, not an exception.
    src = (ROOT / "subtask_graph.py").read_text(encoding="utf-8")
    assert src.count("run_tool_loop(") >= 1
    assert "raise_on_backend_error" not in src


def test_leaf_recursion_keeps_the_default():
    src = (ROOT / "igor_tool_loop.py").read_text(encoding="utf-8")
    i = src.index("_sub = await run_tool_loop(")
    assert "raise_on_backend_error" not in src[i:src.index("tier_resolver=tier_resolver", i)]
