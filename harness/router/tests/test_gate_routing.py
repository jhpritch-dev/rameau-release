"""
Guards for the gate-routing repair (2026-07-16).

THE BUG. route_after_gate read `state.get("subtask_id")` -- but subtask_id is a
SubtaskState field, NOT an AutopilotState one (copy-pasted from a subtask-graph router).
It was therefore ALWAYS None, so the function logged an ERROR and returned "decompose"
BEFORE it ever read `status`. The gate's decision had never been honored on any prompt,
direct_route_node was unreachable dead code that had never executed once, and the
"'subtask_id' key is missing" ERROR at the head of every run was this firing -- not noise.

THE TRAP. The naive one-line fix would have KILLED THE DEMO: the gate classified the CSV
demo prompt as direct_route, so honouring the gate made it skip decomposition entirely.
The broken guard was the only reason the demo worked -- the third accidentally
load-bearing bug in this arc. So the guard fix ships WITH a recalibration.

THE MIS-CALIBRATION. tool_density = matched/token_count is a DENSITY, so it misfired in
OPPOSITE directions: "Implement a binary search in Python" (6 tok, 1 kw -> 0.167) cleared
the 0.10 bar and fanned a trivial task across the fleet, while the CSV demo prompt (21 tok,
1 kw -> 0.048) did not clear it despite having two deliverables. Fixed with a token floor
plus an explicit multi-deliverable signal. Corpus: 3/8 wrong before, 0/10 after.
"""
import ast
import inspect
import pathlib
import typing

import pytest

import autopilot_graph
from autopilot_graph import _gate_node_impl, route_after_gate
from autopilot_state import AutopilotState

SRC = pathlib.Path(__file__).resolve().parent.parent / "autopilot_graph.py"


def _route_of(prompt, warp_seed=None):
    out = _gate_node_impl({"graph_id": "g", "original_prompt": prompt, "warp_seed": warp_seed})
    return (out.get("_gate") or {}).get("route")


# --- the actual bug ----------------------------------------------------------------

def test_direct_route_status_is_honoured():
    """THE regression. Fails before the fix: returns 'decompose' regardless."""
    assert route_after_gate({"status": "DIRECT_ROUTE"}) == "direct_route"


def test_decomposing_status_still_decomposes():
    assert route_after_gate({"status": "DECOMPOSING"}) == "decompose"


def test_route_after_gate_never_reads_subtask_id():
    """subtask_id is a SubtaskState field. Reading it here can only ever yield None, which
    is what made this function a no-op for the graph's entire life. Pinned so a future
    copy-paste from the subtask router cannot silently re-break routing."""
    src = inspect.getsource(route_after_gate)
    tree = ast.parse(src.strip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value == "subtask_id":
                    pytest.fail("route_after_gate reads subtask_id again -- always None here")


@pytest.mark.parametrize("bad", [None, "not-a-dict", 42])
def test_malformed_state_still_defaults_to_decompose(bad):
    """The real guards stay: decompose is the safe default."""
    assert route_after_gate(bad) == "decompose"


@pytest.mark.parametrize("st", [{}, {"status": None}, {"status": 123}])
def test_missing_or_bad_status_defaults_to_decompose(st):
    assert route_after_gate(st) == "decompose"


# --- calibration: the demo is the acceptance test -----------------------------------

DEMO_CODE = "Write a Python function to parse a CSV file and return the sum of a numeric column, with a unit test."


def test_demo_code_prompt_decomposes_on_its_own_merits():
    """If this direct-routes, the recorded demo has no fan-out. No override involved."""
    assert _route_of(DEMO_CODE) == "decompose"


def test_trivial_short_code_does_not_fan_out():
    """The density misfire: 6 tokens, 1 keyword -> 0.167 cleared the 0.10 bar."""
    assert _route_of("Implement a binary search in Python.") == "direct_route"


@pytest.mark.parametrize("prompt", [
    "Summarize the tradeoffs of local LLM inference vs cloud APIs.",   # document task
    "Build an end-to-end ingestion pipeline that reads CSVs, validates rows, transforms them and generates a summary report.",
    "Refactor the authentication module into a service with an API.",
    "Write a parser, with a unit test and documentation.",
    "Write a whitepaper on local inference economics.",
])
def test_prompts_that_must_decompose(prompt):
    assert _route_of(prompt) == "decompose"


@pytest.mark.parametrize("prompt", [
    "What is 2+2?",
    "What is the capital of France?",
    "Write a Python function that reverses a string.",
])
def test_prompts_that_must_not_decompose(prompt):
    assert _route_of(prompt) == "direct_route"


def test_density_needs_a_token_floor():
    assert autopilot_graph._DENSITY_MIN_TOKENS >= 10


def test_bare_and_inside_one_sentence_is_not_a_second_deliverable():
    """The multi-deliverable regex must not trip on ordinary prose. 'the sum of a column
    and return it' is ONE deliverable; anchoring on a comma-conjunction is what prevents
    every 'and' from forcing a fan-out."""
    assert not autopilot_graph._MULTI_DELIVERABLE.search(
        "Write a function that returns the sum of a column and returns it."
    )


def test_comma_with_a_test_is_a_second_deliverable():
    assert autopilot_graph._MULTI_DELIVERABLE.search(DEMO_CODE)


# --- the caller override wins over the heuristic -------------------------------------

def test_route_override_forces_decompose():
    """Belt-and-braces for the recording: works even if the heuristic says otherwise."""
    assert _route_of("What is 2+2?", warp_seed={"route": "decompose"}) == "decompose"


def test_route_override_forces_direct():
    assert _route_of(DEMO_CODE, warp_seed={"route": "direct"}) == "direct_route"


def test_no_override_leaves_the_gate_alone():
    assert _route_of(DEMO_CODE, warp_seed=None) == "decompose"
    assert _route_of(DEMO_CODE, warp_seed={}) == "decompose"


def test_job_runner_accepts_warp_seed():
    """main.py spawns the runner POSITIONALLY -- an arity mismatch surfaces only as a hung
    daemon thread, never as an error."""
    import main
    assert "warp_seed" in inspect.signature(main._autopilot_job_runner).parameters


# --- direct_route_node is about to run for the first time ever -----------------------

def _direct_route_src():
    return inspect.getsource(autopilot_graph.direct_route_node)


def test_direct_route_emits_only_declared_statuses():
    """It returned status "ERROR", which is NOT in the Literal. Nobody noticed because the
    node was unreachable."""
    declared = set(typing.get_args(AutopilotState.__annotations__["status"]))
    src = _direct_route_src()
    tree = ast.parse(src.strip())
    emitted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant):
                    emitted.add(v.value)
    assert emitted, "no literal statuses found -- did the return shape change?"
    assert emitted <= declared, f"direct_route_node emits undeclared status(es): {emitted - declared}"


def test_direct_route_reports_failures_via_error_not_message():
    """/status echoes `error` and never echoes `message`, so a `message` reason is invisible."""
    src = _direct_route_src()
    assert '"message"' not in src, "reason must live in `error`; /status does not echo `message`"


def test_direct_route_does_not_claim_complete_unconditionally():
    """It returned COMPLETE regardless of result.status (PASS|FAIL|ESCALATE|
    FAILED_NEEDS_HUMAN) -- the same green-on-failure lie as the fan-out assembler."""
    src = _direct_route_src()
    assert "_assembly_status(" in src, "must reuse _assembly_status so a failed leg reports PARTIAL"
    assert "result.status" in src, "must actually inspect the leg's status"


def test_direct_route_publishes_its_leg_as_a_subtask():
    """main.py derives subtasks_total from len(state["subtasks"]). direct_route never
    populated it, so /status said subtasks_total=0 with subtasks_completed=1 and the Portal
    rendered "1/0 subtasks" (observed live on v224). The direct leg IS one subtask.
    """
    src = _direct_route_src()
    tree = ast.parse(src.strip())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys if isinstance(k, ast.Constant)}
    assert "subtasks" in keys, (
        'direct_route_node must return a "subtasks" entry or /status reports 1/0'
    )
