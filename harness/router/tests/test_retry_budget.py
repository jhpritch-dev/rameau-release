"""Retry budget: attempt COUNT and wall-clock DEADLINE, and the L5 POST guard.

2026-07-26. The v255 stability bank ran 5 COMPLETE / 1 PARTIAL, and the one PARTIAL was
CONVERGING when it ran out of attempts (`2 of 2 failed` -> `2 of 2` -> `1 of 2`). Raising
MAX_ATTEMPTS 3 -> 5 buys that leg its last step -- but the whole leg, retry loop included,
runs inside the wave's as_completed(timeout=SUBTASK_WALL_CLOCK_TIMEOUT=720s), and
5 * AUTOPILOT_LEG_INNER_TIMEOUT is 1000s. A wave timeout is WORSE than a PARTIAL: it takes
the leg's siblings down with it. So the budget is bounded by BOTH, whichever binds first.
"""
import time as _time

import pytest

import subtask_graph
from subtask_graph import (
    AUTOPILOT_LEG_INNER_TIMEOUT,
    MAX_ATTEMPTS,
    _LEG_RETRY_DEADLINE,
    _spec_is_code_task,
    route_after_verify,
)


class _R:
    """Minimal stand-in for SubtaskResult -- route_after_verify reads .status only."""

    def __init__(self, status):
        self.status = status


class _Spec:
    def __init__(self, deliverable=None, description=""):
        self.deliverable = deliverable
        self.description = description


def _state(**kw):
    base = {
        "result": _R("FAIL"),
        "attempt_count": 1,
        "started_at": _time.monotonic(),
    }
    base.update(kw)
    return base


# --- the invariant that makes the count raise safe -------------------------------------

def test_deadline_is_under_the_wave_net():
    """The leg deadline must sit below the wave's as_completed net, with margin.

    If this ever inverts, the count raise silently reintroduces the wave-timeout risk it
    was paired with this deadline to avoid.
    """
    from autopilot_graph import SUBTASK_WALL_CLOCK_TIMEOUT

    assert _LEG_RETRY_DEADLINE < SUBTASK_WALL_CLOCK_TIMEOUT
    # room for verify/sandbox/escalate work outside the inference call
    assert SUBTASK_WALL_CLOCK_TIMEOUT - _LEG_RETRY_DEADLINE >= 100


def test_count_alone_would_have_blown_the_wave_net():
    """Documents WHY the deadline exists: the arithmetic worst case really does overrun."""
    from autopilot_graph import SUBTASK_WALL_CLOCK_TIMEOUT

    assert MAX_ATTEMPTS * AUTOPILOT_LEG_INNER_TIMEOUT > SUBTASK_WALL_CLOCK_TIMEOUT


# --- routing ---------------------------------------------------------------------------

def test_pass_ends_regardless_of_budget():
    assert route_after_verify(_state(result=_R("PASS"), attempt_count=99)) == "__end__"


def test_fresh_failure_escalates():
    assert route_after_verify(_state(attempt_count=1)) == "escalate"


def test_attempt_4_still_escalates_after_the_raise():
    """The whole point of 3 -> 5: attempt 4 used to be the wall."""
    assert route_after_verify(_state(attempt_count=4)) == "escalate"


def test_count_ceiling_still_binds():
    assert route_after_verify(_state(attempt_count=MAX_ATTEMPTS)) == "failed"


def test_deadline_binds_when_the_leg_is_running_at_the_cap():
    """One slow attempt at the inner cap -> next one cannot fit -> fail cleanly."""
    # 1 attempt, 410s elapsed: est_next = min(410, 200) = 200 -> 610 > 600.
    burned = _time.monotonic() - 410
    assert route_after_verify(_state(attempt_count=1, started_at=burned)) == "failed"


def test_deadline_does_not_bind_with_room_to_spare():
    burned = _time.monotonic() - 30
    assert route_after_verify(_state(attempt_count=1, started_at=burned)) == "escalate"


def test_fast_legs_keep_retrying_well_past_the_naive_cutoff():
    """The lookahead must use the leg's OWN pace, not the pathological inner cap.

    Costing the next attempt at AUTOPILOT_LEG_INNER_TIMEOUT would cut EVERY leg at 400s
    elapsed -- more restrictive than the 3-attempt budget this replaces. A leg averaging
    ~60s/attempt has plenty of headroom at 450s and must still be allowed to retry.
    """
    burned = _time.monotonic() - 450          # 450s over 4 attempts -> est_next ~113s
    assert route_after_verify(_state(attempt_count=4, started_at=burned)) == "escalate"


def test_slow_leg_is_cut_before_a_fast_leg_at_the_same_elapsed():
    """Same clock, different pace -> different decision. That is the whole point."""
    burned = _time.monotonic() - 450
    fast = route_after_verify(_state(attempt_count=4, started_at=burned))
    slow = route_after_verify(_state(attempt_count=1, started_at=burned))
    assert (fast, slow) == ("escalate", "failed")


def test_missing_started_at_falls_back_to_count_only():
    """A state from an older build has no started_at -- must not crash or fail early."""
    assert route_after_verify(_state(attempt_count=1, started_at=None)) == "escalate"


def test_run_subtask_stamps_started_at():
    """The deadline is inert unless the initial state carries the stamp."""
    import inspect

    src = inspect.getsource(subtask_graph.run_subtask)
    assert '"started_at"' in src and "monotonic" in src


# --- L5 POST guard (docs-leg noise) -----------------------------------------------------

@pytest.mark.parametrize("kind", ["code", "tests", "CODE", " Tests "])
def test_code_legs_are_l5_eligible(kind):
    assert _spec_is_code_task(_Spec(deliverable=kind)) is True


@pytest.mark.parametrize("kind", ["code_selftest", "CODE_SELFTEST", " code_selftest "])
def test_selftest_legs_are_l5_eligible(kind):
    """Regression, demo bank 2026-08-07 (graph demobank-3).

    code_selftest was absent from the eligible set, so the L5 sandbox logged
    "skipped non-code leg ... deliverable='code_selftest'" for EVERY code leg in the coding beat
    and L6 then reported passed=True with tests_collected=0. The bank scored 3/3 while one leg had
    shipped a pytest suite that fails 1 of 5 cases and another had shipped no suite at all.

    This kind is the only code kind W3/W4 emit, and it was the one kind no test named -- which is
    how the gap survived review.
    """
    assert _spec_is_code_task(_Spec(deliverable=kind)) is True


def test_kb_grounding_is_the_inverse_of_l5_eligibility():
    """The two predicates must not drift: they disagreed about code_selftest until 2026-08-07."""
    from subtask_graph import _spec_wants_kb_grounding
    for kind in ("code", "tests", "code_selftest"):
        assert _spec_is_code_task(_Spec(deliverable=kind)) is True
        assert _spec_wants_kb_grounding(_Spec(deliverable=kind)) is False
    for kind in ("docs", "analysis", "prose"):
        assert _spec_is_code_task(_Spec(deliverable=kind)) is False
        assert _spec_wants_kb_grounding(_Spec(deliverable=kind)) is True


@pytest.mark.parametrize("kind", ["docs", "analysis", "prose"])
def test_docs_legs_are_not_l5_eligible(kind):
    """The s2 _doc_section leg: its fenced usage EXAMPLE is not Python and never was."""
    assert _spec_is_code_task(_Spec(deliverable=kind)) is False


def test_deliverable_beats_description_keywords():
    """The exact false positive that made 3 takes PARTIAL on 2026-07-23.

    'Write user documentation ... how to invoke the script' matches _CODE_TASK_RE's
    verb-then-noun span. The stamped deliverable is authoritative; prose is not.
    """
    spec = _Spec(
        deliverable="docs",
        description="Write user documentation for the CLI tool. It should explain how to "
                    "invoke the script and what each function does.",
    )
    assert _spec_is_code_task(spec) is False


def test_regex_fallback_when_deliverable_absent():
    """Older graphs have no stamped deliverable -- the keyword scan still has to work."""
    assert _spec_is_code_task(_Spec(description="Implement a function that parses CSV")) is True
    assert _spec_is_code_task(_Spec(description="Write documentation for the module")) is False
