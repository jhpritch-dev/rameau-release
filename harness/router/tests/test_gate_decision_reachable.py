"""
Guards for the gate-decision debt paydown (2026-07-16 audit).

THE BUG. gate_node always returned {"status": ..., "_gate": GateDecision(...)}, but `_gate`
was NOT a field on AutopilotState. LangGraph builds its state channels from the TypedDict,
so it silently DROPPED the key: the whole GateDecision -- route, reason, subtask_count --
was discarded the moment the gate returned. Confirmed against a live state dump, whose
surviving keys were graph_id / trace_id / original_prompt / ... / leg_target / subtasks,
with no _gate anywhere. So `subtask_count` was never "unwired" -- it was UNREACHABLE, and
nothing read `_gate` because nothing could.

WHAT IS DELIBERATELY NOT HERE. No leg recommender. The gate's only non-zero subtask_count
(6, for RFP) is vestigial: RFP bypasses the AI decomposer entirely
(decompose_node -> _build_rfp_dag = 5 sections + 1 coherence pass), via a path that never
consults the gate. And every measured count failure is already fixed -- collapse-to-1 by
minItems=2, MoE 2-vs-3 variance by `legs=N`. Leg count is pinned by the CALLER, because a
keyword heuristic guessing N is a guess and the decoding grammar is a guarantee.
"""
import ast
import inspect
import pathlib
import typing

import pytest

import autopilot_graph
from autopilot_graph import _assembly_status
from autopilot_state import AutopilotState, GateDecision

SRC = pathlib.Path(__file__).resolve().parent.parent / "autopilot_graph.py"


# --- the actual bug: _gate must be a declared field ---------------------------------

def test_gate_is_a_declared_state_field():
    """Absent from the TypedDict => LangGraph drops it => the gate decision is discarded."""
    assert "_gate" in AutopilotState.__annotations__, (
        "_gate missing from AutopilotState: LangGraph will silently drop gate_node's "
        "GateDecision and nothing downstream can ever read it"
    )


def test_gate_gets_a_real_langgraph_channel():
    """The end-to-end proof: declaring the field must actually create a state channel.

    Schema membership alone would not prove LangGraph honors a leading-underscore key.
    """
    g = autopilot_graph.autopilot_graph
    channels = getattr(g, "channels", None) or getattr(getattr(g, "builder", None), "channels", {})
    assert "_gate" in channels, "_gate has no LangGraph channel -- it will still be dropped"
    # leg_target is the known-good control: it is verified to survive on the live router.
    assert "leg_target" in channels


def test_gate_decision_shape_is_unchanged():
    assert set(GateDecision.__annotations__) == {
        "route", "reason", "tool_density", "seq_depth", "subtask_count",
    }


# --- single writer => no reducer ---------------------------------------------------

def test_gate_node_is_the_only_writer_of_gate():
    """_gate is declared WITHOUT an Annotated[] reducer, which is only safe while exactly
    one node writes it. A second writer needs a merge strategy -- fail loudly here first.
    """
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    writers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            # a real write is a dict literal with a "_gate" key, not a mention in a comment
            if isinstance(sub, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "_gate" for k in sub.keys
            ):
                writers.append(node.name)
                break
    assert set(writers) <= {"_gate_node_impl"}, (
        f"_gate is written by {sorted(set(writers))}; with >1 writer the field needs an "
        "Annotated[...] reducer on AutopilotState"
    )


def test_gate_field_has_no_reducer():
    """Last-write-wins is correct for a single-writer, run-once field. An Annotated[]
    here would be cargo-culted from subtasks/completed, which genuinely fan in."""
    assert typing.get_origin(AutopilotState.__annotations__["_gate"]) is not typing.Annotated


# --- the wrapper must publish on EVERY return path ---------------------------------

def test_gate_node_wraps_the_impl_and_publishes():
    """gate_node wraps _gate_node_impl so a future eighth return cannot skip publishing."""
    src = inspect.getsource(autopilot_graph.gate_node)
    assert "_gate_node_impl(state)" in src
    assert "_update_job_gate" in src


def test_update_job_gate_never_raises():
    """Best-effort contract: it runs inside the graph; an exception must not escape."""
    autopilot_graph._update_job_gate("does-not-exist", {"route": "decompose", "reason": "x"})
    autopilot_graph._update_job_gate("", None)
    autopilot_graph._update_job_gate(None, None)


def test_update_job_registry_cannot_carry_the_gate():
    """Documents WHY _update_job_gate is a separate function -- do not merge them.

    _update_job_registry early-returns on subtasks_total <= 0, and at gate time it is 0
    (the gate runs before decompose), so it can never publish the gate decision.
    """
    src = inspect.getsource(autopilot_graph._update_job_registry)
    assert "if subtasks_total <= 0" in src, (
        "the early-return that makes _update_job_registry unusable at gate time is gone -- "
        "re-check whether _update_job_gate is still needed"
    )


# --- the status annotation must not lie again --------------------------------------

def test_partial_is_a_declared_status():
    """_assembly_status has emitted PARTIAL since v220; the Literal did not list it."""
    assert "PARTIAL" in typing.get_args(AutopilotState.__annotations__["status"])


def test_every_status_the_assembler_can_emit_is_declared():
    """Pins emitter and annotation together so they cannot drift apart again."""
    declared = set(typing.get_args(AutopilotState.__annotations__["status"]))
    emitted = {
        _assembly_status({}),                      # COMPLETE
        _assembly_status({"s1": "boom"}),          # PARTIAL
        _assembly_status({}, degraded=True),       # PARTIAL
    }
    assert emitted <= declared, f"assembler emits {emitted - declared}, undeclared in status"


@pytest.mark.parametrize("s", ["FAILED_NEEDS_HUMAN", "DIRECT_ROUTE", "COMPLETE", "FAILED"])
def test_known_statuses_still_declared(s):
    assert s in typing.get_args(AutopilotState.__annotations__["status"])


# --- terminal nodes must never return a status-less update (v227) -------------------

def test_assemble_node_never_returns_a_statusless_update():
    """A bare `return {}` is a PARTIAL state update that sets NOTHING.

    `status` then keeps its last value ("EXECUTING") and assemble edges straight to END, so
    the graph RETURNS a non-terminal status, the job runner copies it verbatim, and the job
    is "running" forever with elapsed frozen. Observed live on v226 graph a1fba388:
        Finished autopilot_graph.invoke (status=EXECUTING)

    Same class as the N<=3 gate dropping `subtasks`: a node return that omits the key the
    graph is steered by. assemble is a TERMINAL node (edge -> END), so every one of its
    returns must carry a status.
    """
    import ast
    import inspect
    src = inspect.getsource(autopilot_graph.assemble_node)
    tree = ast.parse(src.strip())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        if not isinstance(node.value, ast.Dict):
            continue  # e.g. `return _run_async(_assemble_async(state))`
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if "status" not in keys:
            seg = ast.get_source_segment(src, node.value) or ""
            bad.append((node.lineno, " ".join(seg.split())[:60]))
    assert not bad, (
        "assemble_node has dict return(s) with no 'status' -- the graph will END on a "
        "non-terminal status and the job hangs forever:\n"
        + "\n".join(f"  line {ln}: {s}" for ln, s in bad)
    )


def test_every_assemble_status_is_declared():
    """Whatever assemble emits must exist in the status Literal."""
    import ast
    import inspect
    declared = set(typing.get_args(AutopilotState.__annotations__["status"]))
    src = inspect.getsource(autopilot_graph.assemble_node)
    emitted = set()
    for node in ast.walk(ast.parse(src.strip())):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant):
                    emitted.add(v.value)
    assert emitted <= declared, f"assemble emits undeclared status(es): {emitted - declared}"
