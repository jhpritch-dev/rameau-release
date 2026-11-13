"""Igor's tool loop: argument errors come back as structured tool results instead of running the
call, and a malformed tool name is repaired only onto the allowlist.

2026-09-11, Igor review follow-up (finding B6 of the 2026-09-10 harness review, OpenCode s.9.3).
Before this change:
  * arguments that were not valid JSON became {} and the call RAN with no arguments;
  * arguments that parsed to a non-object (a list, a string, a number) were passed on as they
    were, and a delegate_subtask call with a list crashed the loop on args.get;
  * a call missing a required argument ran and failed inside the tool, or did the wrong thing;
  * "functions.grep_repo" or "Grep_Repo" was refused as out-of-subset.
Fakes only, no network.
"""
import asyncio
import json
from pathlib import Path

import pytest

import igor_tool_loop as itl

_ROOT = Path(__file__).resolve().parents[1]


def test_module_comes_from_this_tree():
    assert Path(itl.__file__).resolve().parent == _ROOT


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


def _call(name, arguments, cid="c0"):
    return {"id": cid, "function": {"name": name, "arguments": arguments}}


def _reply(*calls):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": list(calls)},
                         "finish_reason": "tool_calls"}]}


def _final(text="done"):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _run(backend, gateway, max_iterations=6):
    return asyncio.run(itl.run_tool_loop(
        backend_dispatch_fn=backend, tier="no-such-tier",
        messages=[{"role": "user", "content": "check the repo"}],
        temperature=0.0, max_tokens=None, caller_thinking=False,
        gateway_call_fn=gateway, max_iterations=max_iterations, total_turn_timeout_s=60.0,
        request_id="t-args"))


def _tool_results(backend, i=1):
    return {m["tool_call_id"]: json.loads(m["content"])
            for m in backend.calls[i]["messages"] if m.get("role") == "tool"}


def test_invalid_json_arguments_are_not_executed():
    be, gw = Backend([_reply(_call("grep_repo", '{"pattern": "foo"')), _final()]), Gateway()
    out = _run(be, gw)
    assert gw.calls == [], "a call whose arguments did not parse must not run with {}"
    body = _tool_results(be)["c0"]
    assert body["kind"] == "bad_arguments"
    assert "not valid JSON" in body["error"] and "NOT executed" in body["error"]
    assert "pattern" in body["expected"]["required"]
    assert out["_bifrost_tool_loop"]["reason"] == "completed"


@pytest.mark.parametrize("arguments", ['["foo"]', '"foo"', "42", ["foo"], 42])
def test_non_object_arguments_are_not_executed(arguments):
    be, gw = Backend([_reply(_call("grep_repo", arguments)), _final()]), Gateway()
    _run(be, gw)
    assert gw.calls == []
    body = _tool_results(be)["c0"]
    assert body["kind"] == "bad_arguments" and "JSON object" in body["error"]


def test_delegate_with_list_arguments_does_not_crash_the_loop():
    """Before: args=["x"] reached the delegate fence, whose args.get raised AttributeError."""
    be, gw = Backend([_reply(_call("delegate_subtask", '["x"]')), _final()]), Gateway()
    out = _run(be, gw)
    assert gw.calls == []
    assert _tool_results(be)["c0"]["kind"] == "bad_arguments"
    assert out["_bifrost_tool_loop"]["reason"] == "completed"


@pytest.mark.parametrize("name,arguments,missing", [
    ("delegate_subtask", {"leaf_alias": "bifrost-granite"}, "subtask"),
    ("delegate_subtask", {"subtask": "look it up"}, "leaf_alias"),
    ("delegate_subtask", {"leaf_alias": "bifrost-granite", "subtask": None, "mode": "tools"}, "subtask"),
    ("tool_output_page", {}, "id"),
])
def test_missing_required_argument_of_an_in_loop_tool_is_not_executed(name, arguments, missing):
    """The loop serves or fences these itself. Before: a mode=tools delegate with no subtask
    ran a nested loop on an empty message; a reasoning delegate reached the gateway."""
    be, gw = Backend([_reply(_call(name, arguments)), _final()]), Gateway()
    _run(be, gw)
    assert gw.calls == []
    body = _tool_results(be)["c0"]
    assert body["kind"] == "bad_arguments"
    assert f"missing required argument(s): {missing}" in body["error"]


def test_gateway_tools_keep_gateway_validation():
    """A gateway-served tool with a missing required argument is still dispatched: the gateway
    validates it against the tool signature (the loop does not second-guess it)."""
    be, gw = Backend([_reply(_call("grep_repo", {})), _final()]), Gateway()
    _run(be, gw)
    assert gw.calls == [("grep_repo", {})]


@pytest.mark.parametrize("arguments", ["", "   ", None, {}])
def test_empty_arguments_still_mean_no_arguments(arguments):
    """Regression guard: a tool with no required argument still runs on "", None or {}."""
    be, gw = Backend([_reply(_call("git_status", arguments)), _final()]), Gateway()
    _run(be, gw)
    assert gw.calls == [("git_status", {})]


def test_valid_call_unchanged():
    be, gw = Backend([_reply(_call("grep_repo", '{"pattern": "foo", "repo": "autoagent"}')), _final()]), Gateway()
    out = _run(be, gw)
    assert gw.calls == [("grep_repo", {"pattern": "foo", "repo": "autoagent"})]
    assert out["_bifrost_tool_loop"]["successful_tool_calls"] == 1


@pytest.mark.parametrize("name", ["functions.grep_repo", "Grep_Repo", " grep_repo ", "grep-repo",
                                  "tools.GREP-REPO"])
def test_malformed_name_is_repaired_onto_the_allowlist(name):
    be, gw = Backend([_reply(_call(name, {"pattern": "foo"})), _final()]), Gateway()
    out = _run(be, gw)
    assert gw.calls == [("grep_repo", {"pattern": "foo"})]
    assert out["_bifrost_tool_loop"]["tools_called"] == ["grep_repo"]


@pytest.mark.parametrize("name", ["functions.write_file", "Write_File", "run-python",
                                  "tools.autopilot_submit", "no_such_tool"])
def test_repair_never_widens_the_allowlist(name):
    be, gw = Backend([_reply(_call(name, {"path": "x", "content": "y", "code": "z"})), _final()]), Gateway()
    _run(be, gw)
    assert gw.calls == []
    assert _tool_results(be)["c0"]["kind"] == "out_of_subset"


def test_bad_arguments_do_not_use_a_delegate_width_slot():
    """A malformed delegate call is answered before the width counter moves."""
    bad = [_call("delegate_subtask", {"leaf_alias": "x"}, cid=f"b{k}") for k in range(itl.DELEGATE_WIDTH_CAP)]
    good = _call("delegate_subtask", {"leaf_alias": "bifrost-granite", "subtask": "hi"}, cid="g0")
    be, gw = Backend([_reply(*bad, good), _final()]), Gateway()
    _run(be, gw)
    res = _tool_results(be)
    assert all(res[f"b{k}"]["kind"] == "bad_arguments" for k in range(itl.DELEGATE_WIDTH_CAP))
    assert res["g0"].get("kind") != "delegate_width_cap"
    assert gw.calls == [("delegate_subtask", {"leaf_alias": "bifrost-granite", "subtask": "hi"})]
