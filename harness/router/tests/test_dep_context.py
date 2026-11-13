"""
Guards for dependency-output injection -- making `dependencies` mean something.

THE BUG. `dep_count`/`dag_edges` gated SCHEDULING ORDER ONLY. fan_out_node submitted the
UNMODIFIED spec and run_subtask(spec) received only that spec; nothing anywhere injected an
upstream leg's output into a dependent. So a leg declaring dependencies=["s1"] ran after s1
and was exactly as blind. Measured: dependencies=[] on all 37 spec dumps, every run.

The consequence was structural -- a unit-test leg could not see the function it was testing
and honestly refused:

    status='FAILED_NEEDS_HUMAN' tier='2_devstral' verification_level_reached=2
    output='I do not have the information to create a self-contained Python unit test
            script for `sum_numeric_column` based on the provided context.'

The stub detector then failed it (description says "function", output has no code, <200
chars) -> escalate -> exhausted -> PARTIAL, on every v225 demo run.

PRIOR ART: fan_out_node's RFP coherence block has always collected completed[dep].output
this way -- but only for _is_rfp_task on tier 2.5/2, and only via the _run_rfp_coherence
bypass. _with_dep_context generalizes it.
"""
import pytest

from autopilot_graph import _with_dep_context
from bifrost_message import SubtaskResult, SubtaskSpec, compute_checksum
from subtask_graph import (
    _OUTPUT_RESERVE_TOKENS,
    _build_messages,
    _estimate_envelope_tokens,
)

IMPL = "def sum_csv_column(path: str, column: str) -> float:\n    return 6.0\n"
DESC = "Write a unit test for sum_csv_column."


def _spec(deps, budget=8192, desc=DESC, tier="1b"):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s2", description=desc,
        acceptance_criteria=["asserts the sum is 6.0"], assigned_tier=tier,
        dependencies=deps, context_budget_tokens=budget,
    )


def _res(sid, out):
    return SubtaskResult(
        type="subtask_result", subtask_id=sid, graph_id="g", status="PASS", output=out,
        output_checksum=compute_checksum(out), verification_level_reached=5,
        tier_used="1b", tokens_used=1, cloud_cost_usd=0.0,
    )


# --- the no-op guarantee ------------------------------------------------------------

def test_no_dependencies_returns_the_identical_spec():
    """THE safety property. Every leg emits dependencies=[] today (37/37 measured), so
    until the decomposer declares one, fan_out submits a bit-identical object and the
    whole path is unchanged. `is` -- not merely equal."""
    s = _spec([])
    assert _with_dep_context(s, {}) is s


def test_no_dependencies_leaves_dep_context_none():
    assert _with_dep_context(_spec([]), {}).dep_context is None


# --- injection ----------------------------------------------------------------------

def test_dependency_output_is_injected():
    out = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", IMPL)})
    assert "sum_csv_column" in out.dep_context


def test_multiple_dependencies_all_appear():
    out = _with_dep_context(
        _spec(["s1", "s3"]),
        {"s1": _res("s1", IMPL), "s3": _res("s3", "HELPER_MARKER")},
    )
    assert "sum_csv_column" in out.dep_context and "HELPER_MARKER" in out.dep_context


def test_description_is_never_mutated():
    """The stub detector reads spec.description for _is_code_task, and L3 scores keyword
    coverage against it. Pasting an upstream implementation in there would corrupt
    verification -- which is why dep_context is a separate field, not a description append.
    """
    out = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", IMPL)})
    assert out.description == DESC
    assert IMPL not in out.description


# --- upstream missing / failed ------------------------------------------------------

def test_missing_dependency_is_omitted_not_fatal():
    out = _with_dep_context(_spec(["s1", "s9"]), {"s1": _res("s1", IMPL)})
    assert "sum_csv_column" in out.dep_context
    assert "s9" not in out.dep_context


def test_all_dependencies_missing_returns_spec_unchanged():
    assert _with_dep_context(_spec(["s9"]), {}).dep_context is None


def test_empty_upstream_output_is_omitted():
    assert _with_dep_context(_spec(["s1"]), {"s1": _res("s1", "   ")}).dep_context is None


# --- budget: must not trip the envelope gate ----------------------------------------

@pytest.mark.parametrize("size", [2_000, 50_000, 200_000])
def test_injection_never_trips_the_envelope_gate(size):
    """The gate FAILS a leaf leg ("re-decompose smaller") rather than truncating
    (subtask_graph ~612). Injecting context we just added must not kill the leg we were
    trying to help. Asserted against the REAL estimator and budget, not a magic number.
    """
    s = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", "x" * size)})
    envelope = _estimate_envelope_tokens(_build_messages(s, [], []))
    assert envelope <= s.context_budget_tokens - _OUTPUT_RESERVE_TOKENS, (
        f"envelope {envelope} tok exceeds the gate limit for a {size}-char upstream -- "
        "truncation is too generous and the gate will fail the leg"
    )


def test_oversized_upstream_is_marked_as_truncated():
    out = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", "x" * 200_000)})
    assert "truncated" in out.dep_context


def test_no_room_left_injects_nothing_rather_than_trip_the_gate():
    """A description that already eats the budget: inject nothing and let the leg fail on
    its own terms, instead of guaranteeing an envelope-gate failure."""
    s = _spec(["s1"], budget=2048, desc="D" * 30_000)
    assert _with_dep_context(s, {"s1": _res("s1", IMPL)}).dep_context is None


# --- it actually reaches the model --------------------------------------------------

def test_dep_context_reaches_the_messages():
    s = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", IMPL)})
    msgs = _build_messages(s, [], [])
    assert any("sum_csv_column" in (m.get("content") or "") for m in msgs)


def test_dep_context_precedes_the_task_message():
    """Read the upstream work first, then the task."""
    s = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", IMPL)})
    contents = [m.get("content") or "" for m in _build_messages(s, [], [])]
    dep_i = next(i for i, c in enumerate(contents) if "sum_csv_column" in c)
    task_i = next(i for i, c in enumerate(contents) if c == DESC)
    assert dep_i < task_i


def test_no_dep_context_adds_no_message():
    """No dependencies -> the envelope is byte-identical to before this feature."""
    plain = _build_messages(_spec([]), [], [])
    assert not any("depends on" in (m.get("content") or "") for m in plain)


# --- survives escalation ------------------------------------------------------------

def test_dep_context_survives_the_escalate_rebuild():
    """escalate_node rebuilds via SubtaskSpec(**spec.model_dump(), ...), so dep_context
    rides along for free -- otherwise the retry would go blind and refuse again."""
    s = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", IMPL)})
    rebuilt = SubtaskSpec(**{**s.model_dump(), "assigned_tier": "2_devstral"})
    assert rebuilt.dep_context == s.dep_context


# --- the decomposer must no longer forbid what it needs -----------------------------

def test_decomposer_prompt_no_longer_denies_dependency_context():
    """It said "Do NOT assume the executor can see other subtasks" with no carve-out, while
    also telling the model to set dependencies=[] on EVERY subtask -- so a test leg could
    never be written correctly."""
    from autopilot_graph import _DECOMPOSER_SYSTEM
    assert "dependencies" in _DECOMPOSER_SYSTEM
    assert "EXCEPT" in _DECOMPOSER_SYSTEM, "the THIN-LEAF rule needs the dependency carve-out"


def test_decomposer_prompt_names_the_unit_test_case():
    """The case the model never recognized on its own: a test leg must declare a dependency
    on the implementation it tests.

    Asserted by INTENT, not by literal phrasing. The original test required the exact strings
    "unit test" and 'dependencies=["s1"]'. The W3/W4 code_selftest rewrite (e60fe1d, a91a3b0,
    2026-08-03) restated the same rule in stronger terms -- "A pytest/test-suite subtask goes
    on 2_qwen_coder and MUST depend on the implementation it tests", and an exemplar reading
    dependencies=["<that leg>"] -- so the literals vanished while the guidance got better, and
    the test failed on a prompt that had actually improved. Matching wording that tightly makes
    a test fire on rewording rather than on removal. These assertions still fail if the rule
    itself is dropped.
    """
    from autopilot_graph import _DECOMPOSER_SYSTEM
    low = _DECOMPOSER_SYSTEM.lower()

    # 1. it must name the test-suite case at all
    assert any(t in low for t in ("unit test", "test suite", "pytest")), \
        "decomposer prompt no longer names the test-leg case"

    # 2. it must tie that case to declaring a dependency
    assert "depend" in low, "decomposer prompt no longer mentions dependencies"
    assert any(t in low for t in ("must depend", "needs dependencies", "needs a\ndependency",
                                  "needs a dependency")), \
        "decomposer prompt no longer REQUIRES a test leg to depend on its implementation"

    # 3. it must show a non-empty dependencies exemplar, not only dependencies=[]
    import re
    assert re.search(r'dependencies=\[\s*"[^"]+"', _DECOMPOSER_SYSTEM), \
        "decomposer prompt lost its non-empty dependencies=[...] exemplar"
