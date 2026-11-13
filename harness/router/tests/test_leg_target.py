"""
Guards for the caller-pinned leg count (`legs` on /autopilot/submit).

WHY THIS EXISTS. The decomposer is gpt-oss-igor-65k (gpt-oss:120b), which is MoE and
returned 3 subtasks on one call and 1 on the next from IDENTICAL input at temperature 0.
A caller who needs a reproducible fan-out -- a recording, a benchmark, an eval -- cannot
get one by asking the model nicely. `legs` pins the ollama decoding grammar
(minItems == maxItems), so the count is not a request, it is unviolatable.

Deliberately NOT the default: minItems=2 stays the documented contract ("Produce AT LEAST
2 ... Never collapse to a single subtask"; "aim for 3" is soft). Pinning every task to N
would pad genuinely 2-part work with makework.

NOTE the boundary: `legs` is validated in main.py's submit handler, not in the worker
thread. A bad value must 400, not raise int() inside a daemon thread where the job would
simply hang with no error.
"""
import pytest

from autopilot_graph import MAX_SUBTASKS, _decompose_schema, _leg_bounds


# --- bounds -----------------------------------------------------------------------

def test_none_keeps_the_documented_default():
    """Unpinned callers must see exactly today's behavior."""
    assert _leg_bounds(None) == (2, MAX_SUBTASKS)


def test_pinning_makes_min_equal_max():
    """min == max is what makes the count exact rather than a floor."""
    assert _leg_bounds(3) == (3, 3)


@pytest.mark.parametrize("n", [2, 3, 4, 5, MAX_SUBTASKS])
def test_in_range_values_pin_exactly(n):
    assert _leg_bounds(n) == (n, n)


@pytest.mark.parametrize("low", [1, 0, -5])
def test_below_two_is_clamped_not_honoured(low):
    """legs=1 would re-open the single-subtask collapse the N<=3 gate used to cause."""
    assert _leg_bounds(low) == (2, 2)


def test_above_max_is_clamped():
    """raw_subtasks[:MAX_SUBTASKS] would silently truncate a larger ask downstream."""
    assert _leg_bounds(MAX_SUBTASKS + 50) == (MAX_SUBTASKS, MAX_SUBTASKS)


# --- the grammar actually carries it ----------------------------------------------

def test_schema_pins_the_count():
    s = _decompose_schema(require_function_name=True, leg_target=3)["properties"]["subtasks"]
    assert s["minItems"] == 3 and s["maxItems"] == 3


def test_schema_default_is_unchanged_by_this_feature():
    """Regression: adding `legs` must not alter the unpinned path."""
    s = _decompose_schema(require_function_name=True)["properties"]["subtasks"]
    assert s["minItems"] == 2
    assert s["maxItems"] == MAX_SUBTASKS


def test_pinning_does_not_disturb_the_rest_of_the_grammar():
    """The function_name requirement and tier enum must survive a pinned schema.

    Dropping function_name is what resurrects the prose-essay bug.
    """
    s = _decompose_schema(require_function_name=True, leg_target=3)
    item = s["properties"]["subtasks"]["items"]
    assert "function_name" in item["required"]
    assert item["properties"]["assigned_tier"]["enum"] == ["1b", "1a-hearth", "2_devstral", "2_qwen_coder"]


def test_document_tasks_can_also_be_pinned():
    s = _decompose_schema(require_function_name=False, leg_target=3)
    assert s["properties"]["subtasks"]["minItems"] == 3
    assert "function_name" not in s["properties"]["subtasks"]["items"]["required"]


# --- state plumbing ---------------------------------------------------------------

def test_run_autopilot_accepts_leg_target():
    import inspect
    from autopilot_graph import run_autopilot
    assert "leg_target" in inspect.signature(run_autopilot).parameters


def test_state_declares_leg_target():
    from autopilot_state import AutopilotState
    assert "leg_target" in AutopilotState.__annotations__


def test_decompose_reads_leg_target_from_state():
    """The whole chain is pointless if the decompose call ignores the state field."""
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "autopilot_graph.py"
    text = src.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_decompose_async":
            seg = ast.get_source_segment(text, node) or ""
            assert 'state.get("leg_target")' in seg, (
                "_decompose_async must read leg_target from state, or `legs` is inert"
            )
            return
    pytest.fail("_decompose_async not found")


def test_job_runner_accepts_leg_target():
    """main.py spawns the runner positionally -- an arity mismatch would only surface
    at runtime, inside a daemon thread, as a hung job."""
    import inspect
    import main
    assert "leg_target" in inspect.signature(main._autopilot_job_runner).parameters
