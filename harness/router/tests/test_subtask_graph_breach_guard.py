"""F-4 hardening (2026-09-11, John): a tool-gate breach in a subtask leg is contained by TYPE.

Before this, subtask_graph had NO breach detection at all. A ToolGateBreach raised out of the
leg's tool loop landed in _execute_async's catch-all and became an error STRING. It stayed
terminal only because route_after_execute's _TRANSIENT substring list happened not to match
the breach message. A reworded message (or a broader marker added to _TRANSIENT) would have
sent the breach into the retry / escalate path: the same leg re-run, possibly on another tier.

Now _execute_async stamps error_kind="tool_gate_breach" from an isinstance check, and
route_after_execute ends the leg on that field BEFORE any text is read.

No model calls: run_tool_loop is replaced by a fake that raises, and every model entry point
and the httpx clients are replaced by guards that record a call and raise.
"""
import asyncio
import logging

import httpx
import pytest

import igor_tool_loop
import main  # noqa: F401 -- pre-import: _execute_async does `from main import dispatch_to_tier`
import subtask_graph
from bifrost_message import SubtaskSpec
from igor_tool_loop import ToolGateBreach
from subtask_graph import failed_node, route_after_execute

# The literal, not the module constant: this file must COLLECT on bf3c28a and fail there on
# behavior. test_classifier_type_match pins the constant to it.
BREACH = "tool_gate_breach"

# The breach text today, and a REWORDED one carrying the markers route_after_execute's
# _TRANSIENT list keys on ("502 ", "ReadTimeout"). The reworded one escalated on bf3c28a.
CANONICAL = "denied/unknown tool 'write_file' executed past gate (request_id=g-breach:s1)"
REWORDED = "gate hole: tool 'write_file' ran anyway; upstream said 502 Bad Gateway (ReadTimeout)"

# Not in _LEAF_TIERS (no envelope gate); deliverable=code opts out of the KB call.
TIER = "2_qwen_coder"


def _spec():
    return SubtaskSpec(
        type="subtask_spec", graph_id="g-breach", subtask_id="s1",
        description="Implement add(a, b)", acceptance_criteria=["returns a + b"],
        assigned_tier=TIER, deliverable="code",
    )


def _state(**kw):
    base = {
        "spec": _spec(), "result": None, "attempt_count": 0, "tier_history": [],
        "context_accumulator": [], "verification_level": 0, "cloud_cost_usd": 0.0,
        "error": None, "thinking_cache": [], "started_at": None,
    }
    base.update(kw)
    return base


@pytest.fixture
def leg(monkeypatch):
    """Drive the tool-loop leg with a fake run_tool_loop; forbid every model/network path."""
    blocked = []
    loop_calls = []

    def _guard(name):
        def _refuse(*a, **k):
            blocked.append(name)
            raise AssertionError(f"{name} called in a no-model test")
        return _refuse

    async def _aguard_kb(*a, **k):
        blocked.append("_query_kb")
        return []

    async def _no_event(*a, **k):
        return None

    for name in ("_call_tier", "ollama_chat_completion", "anthropic_chat_completion"):
        monkeypatch.setattr(subtask_graph, name, _guard(name))
    monkeypatch.setattr(httpx.AsyncClient, "send", _guard("httpx.AsyncClient.send"))
    monkeypatch.setattr(httpx.Client, "send", _guard("httpx.Client.send"))
    monkeypatch.setattr(subtask_graph, "_query_kb", _aguard_kb)
    monkeypatch.setattr(subtask_graph, "_write_event", _no_event)
    monkeypatch.setattr(subtask_graph, "_leg_should_use_tool_loop", lambda _t: True)
    monkeypatch.setattr(igor_tool_loop, "make_gateway_caller", lambda *a, **k: None)

    def raising(exc):
        async def _fake_loop(**kwargs):
            loop_calls.append(kwargs.get("request_id"))
            raise exc
        monkeypatch.setattr(igor_tool_loop, "run_tool_loop", _fake_loop)

    return {"raising": raising, "blocked": blocked, "loop_calls": loop_calls}


def _execute(state):
    return asyncio.run(subtask_graph._execute_async(state))


# --- a breach is contained whatever its message says ------------------------------------

def test_reworded_breach_state_routes_to_failed_not_escalate():
    """The core regression: text carrying "502 " and "ReadTimeout" escalated on bf3c28a."""
    st = _state(error=f"execute [{TIER}]: ToolGateBreach: {REWORDED}",
                error_kind=BREACH, attempt_count=1)
    assert route_after_execute(st) == "failed"


@pytest.mark.parametrize("msg", [CANONICAL, REWORDED, ""])
def test_execute_classifies_breach_by_type(leg, msg):
    leg["raising"](ToolGateBreach(msg))
    st = _state()
    out = _execute(st)
    assert out["error_kind"] == BREACH
    assert "ToolGateBreach" in out["error"]
    assert route_after_execute({**st, **out}) == "failed"
    assert leg["blocked"] == []


def test_breach_subclass_is_contained(leg):
    class _NarrowBreach(ToolGateBreach):
        pass

    leg["raising"](_NarrowBreach(REWORDED))
    st = _state()
    out = _execute(st)
    assert out["error_kind"] == BREACH
    assert route_after_execute({**st, **out}) == "failed"


def test_breach_is_terminal_even_with_attempts_left():
    for attempt in range(1, subtask_graph.MAX_ATTEMPTS):
        st = _state(error=f"execute [{TIER}]: ToolGateBreach: {REWORDED}",
                    error_kind=BREACH, attempt_count=attempt)
        assert route_after_execute(st) == "failed"


def test_failed_node_carries_the_structured_kind():
    st = _state(error=f"execute [{TIER}]: ToolGateBreach: {CANONICAL}",
                error_kind=BREACH, attempt_count=1, tier_history=[TIER])
    res = failed_node(st)["result"]
    assert res.status == "FAILED_NEEDS_HUMAN"
    assert res.escalation_artifact["error_kind"] == BREACH
    assert "ToolGateBreach" in res.escalation_artifact["error"]


def test_whole_leg_breach_is_one_attempt_and_no_model_call(leg):
    """End to end through the compiled LangGraph: execute -> failed -> END.

    On bf3c28a the reworded message routed execute -> escalate -> distill -> execute ... up to
    MAX_ATTEMPTS, re-running the tool loop after a breach.
    """
    leg["raising"](ToolGateBreach(REWORDED))
    res = subtask_graph.run_subtask(_spec())
    assert res.status == "FAILED_NEEDS_HUMAN"
    assert res.escalation_artifact["error_kind"] == BREACH
    assert res.escalation_artifact["attempts"] == 1
    assert len(leg["loop_calls"]) == 1, leg["loop_calls"]
    assert leg["blocked"] == []


# --- ordinary failures are unchanged ----------------------------------------------------

def test_transient_ordinary_failure_still_escalates(leg):
    leg["raising"](RuntimeError("upstream 502 Bad Gateway"))
    st = _state()
    out = _execute(st)
    assert out.get("error_kind") is None
    assert route_after_execute({**st, **out}) == "escalate"


def test_non_transient_ordinary_failure_still_fails_without_a_kind(leg):
    leg["raising"](ValueError("bad tier config"))
    st = _state()
    out = _execute(st)
    assert out.get("error_kind") is None
    merged = {**st, **out}
    assert route_after_execute(merged) == "failed"
    assert "error_kind" not in failed_node(merged)["result"].escalation_artifact


def test_state_without_the_new_key_routes_as_before():
    """A state built by an older caller has no error_kind at all."""
    assert route_after_execute({"error": "execute [x]: RemoteProtocolError: boom",
                                "attempt_count": 1}) == "escalate"
    assert route_after_execute({"error": "execute [x]: KeyError: 'a'",
                                "attempt_count": 1}) == "failed"
    assert route_after_execute({"error": None}) == "verify"


# --- the classifier ---------------------------------------------------------------------

def test_classifier_type_match():
    assert subtask_graph._ERROR_KIND_TOOL_GATE_BREACH == BREACH
    assert subtask_graph._is_tool_gate_breach(ToolGateBreach("x")) is True
    assert subtask_graph._is_tool_gate_breach(RuntimeError("ToolGateBreach: executed past gate")) is False
    assert subtask_graph._is_tool_gate_breach(igor_tool_loop.ToolLoopBackendError(
        "OTHER", "t", 0, None)) is False


def test_classifier_name_fallback_is_logged(caplog):
    """One-release fallback: a class NAMED ToolGateBreach that is not the imported one."""
    class ToolGateBreach(RuntimeError):  # noqa: N818 -- deliberately shadows the real name
        pass

    with caplog.at_level(logging.WARNING, logger=subtask_graph.log.name):
        assert subtask_graph._is_tool_gate_breach(ToolGateBreach("x")) is True
    assert any("matched by class NAME" in r.getMessage() for r in caplog.records)
