"""
Guard for defect 6: a tool loop that GIVES UP must not answer for the leg.

The 2026-07-16 diagnosis said "a pure codegen subtask should not be running a tool loop at
all" and prescribed suppressing it. Audit says that trades a demo beat for a symptom:

  - `_leg_should_use_tool_loop` keys ONLY on tier capability -- measured live, it returns
    True for 1b, 1a-hearth and 2_devstral (all report tool_use=True, mcp_gateway_url is
    set, the kill switch is unset). So it is effectively always-on.
  - But `tools_called` / `_bifrost_tool_loop` is REAL and is the Portal's tool-use
    evidence -- the same diagnosis lists it under VERIFIED GOOD.
  - And it did not reproduce on v219/v220: zero "iteration limit" hits.

The actual harm is narrower. When run_tool_loop gives up (iteration_cap /
no_successful_tool_exec / loop_timeout / backend_error) it returns an APOLOGY STRING as
`content` -- "The tool execution loop reached its iteration limit before reaching a final
answer" -- and subtask_graph handed that to the verifier as the leg's ANSWER. It then fails
verification, escalates, and distills a leg that never answered.

So: keep the loop, refuse its give-up text, retry once without tools. Legs are
self-contained by the THIN-LEAF RULE, so a plain call can answer them.
"""
import ast
import inspect
import pathlib

import pytest

import subtask_graph

SRC = pathlib.Path(__file__).resolve().parent.parent / "subtask_graph.py"

# The reasons run_tool_loop sets on _bifrost_tool_loop when it does NOT complete.
GIVE_UP_REASONS = ["iteration_cap", "no_successful_tool_exec", "loop_timeout", "backend_error"]


def _execute_node_src():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and "run_tool_loop" in (
            ast.get_source_segment(src, node) or ""
        ):
            return ast.get_source_segment(src, node) or ""
    pytest.fail("no function calling run_tool_loop found")


def test_give_up_reason_is_checked():
    """The loop's own reason field is the signal -- it is always set."""
    src = _execute_node_src()
    assert "_loop_reason" in src
    assert '"completed"' in src, "must distinguish completed from a give-up"


def test_gives_up_falls_back_to_a_plain_call():
    """On give-up we must re-ask WITHOUT tools, not ship the apology."""
    src = _execute_node_src()
    idx = src.find("_loop_reason not in")
    assert idx != -1, "no give-up branch"
    tail = src[idx:]
    assert "_call_tier(" in tail, "give-up branch must fall back to the plain _call_tier path"


def test_tool_loop_is_NOT_suppressed_by_tier_or_task():
    """Regression against the prescribed-but-wrong fix.

    Suppressing the loop for codegen legs would remove `tools_called`, which the Portal
    reads as tool-use evidence. The loop must still RUN; only its give-up text is refused.
    """
    gate_src = inspect.getsource(subtask_graph._leg_should_use_tool_loop)
    for smell in ("function_name", "_doc_section", "codegen", "is_code"):
        assert smell not in gate_src, (
            f"tool-loop gate now keys on {smell!r} -- that suppresses the loop for code "
            "legs and kills the tools_called demo beat. Refuse the give-up TEXT instead."
        )


def test_fallback_is_flagged_for_telemetry():
    assert "fell_back_to_plain_call" in _execute_node_src()


@pytest.mark.parametrize("reason", GIVE_UP_REASONS)
def test_every_give_up_reason_is_non_completed(reason):
    """Pins the contract: the branch triggers on anything that is not 'completed'.

    A `not in (None, "completed")` test covers every current AND future give-up reason,
    which an explicit allow-list of the four would not.
    """
    assert reason != "completed"
    src = _execute_node_src()
    assert 'not in (None, "completed")' in src, (
        "the give-up branch must be an is-not-completed test, so a NEW terminal reason "
        "added to igor_tool_loop is caught automatically rather than silently shipping "
        "its apology text as a leg answer"
    )
