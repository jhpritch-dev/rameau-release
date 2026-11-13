"""L2 refusal gate is ADVISORY when an EXECUTED gate has already passed the artifact.

DESIGN-verification-harness-fallbacks-2026-08-10 s6. The rule being pinned is the evidence
hierarchy from s1: a gate inspecting a PROXY for the artifact (a regex over prose) must not
overrule a gate that inspected the ARTIFACT ITSELF (running it).

These pin BEHAVIOR, not detection. What MATCHES is ab96f87's business and is covered by the
boundary tests in this same file; what a match COSTS is covered here.
"""
import pytest

import subtask_graph
from bifrost_message import SubtaskResult, compute_checksum

_ADV_DESC = "Implement a function that adds two numbers"

# Refusal phrasing in the PROSE, real code in a FENCE. _REFUSAL_FENCE_RE strips the fence before
# matching, so L2 fires on the prose while L2.5 still sees a function and stays quiet -- which is
# what isolates L2 as the gate under test.
_ADV_OUTPUT = (
    "I cannot be certain this is what you wanted, but here is the implementation:\n"
    "```python\n"
    "def add(a, b):\n"
    "    return a + b\n"
    "def test_add():\n"
    "    assert add(2, 2) == 4\n"
    "```\n"
)
_CLEAN_OUTPUT = _ADV_OUTPUT.replace(
    "I cannot be certain this is what you wanted, but here is the implementation:",
    "Here is the implementation:")


class _AdvSpec:
    def __init__(self):
        self.description = _ADV_DESC
        self.subtask_id = "s1"
        self.graph_id = "g1"
        self.deliverable = "code_selftest"


def _state(output):
    res = SubtaskResult(
        type="subtask_result", subtask_id="s1", graph_id="g1", status="PASS",
        output=output, output_checksum=compute_checksum(output),
        verification_level_reached=0, tier_used="1b", tokens_used=1,
        escalation_artifact={},
    )
    return {"spec": _AdvSpec(), "result": res}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # The QM pass-1 early exit would return before L2 is ever reached.
    monkeypatch.setattr(subtask_graph, "_EVALUATOR_TWO_PASS", False, raising=False)
    # Ensure the L5 branch consults the sandbox rather than short-circuiting on the level gate.
    monkeypatch.setattr(subtask_graph, "_MAX_VERIFY_LEVEL", 6, raising=False)


def _pin_l5(monkeypatch, ran, passed, summary="L5: stub"):
    monkeypatch.setattr(
        subtask_graph, "_l5_from_sandbox",
        lambda *a, **k: {"ran": ran, "passed": passed, "summary": summary, "evidence": ""},
        raising=False,
    )


def test_refusal_is_OVERRULED_when_the_sandbox_executed_and_passed(monkeypatch):
    """The point of the change. The artifact ran and passed; a regex over the preamble
    must not fail the leg."""
    _pin_l5(monkeypatch, ran=True, passed=True)
    out = subtask_graph.verify_subtask_node(_state(_ADV_OUTPUT))
    assert out["result"].status == "PASS"
    assert out["verification_level"] == 5


def test_refusal_STANDS_when_no_executed_gate_ran(monkeypatch):
    """The conservative branch. No execution happened, so nothing outranks the textual
    finding -- same verdict and same artifact shape as before the change."""
    _pin_l5(monkeypatch, ran=False, passed=True)
    out = subtask_graph.verify_subtask_node(_state(_ADV_OUTPUT))
    assert out["result"].status == "FAIL"
    assert out["verification_level"] == 2
    assert "L2: output matched the refusal detector" in out["result"].escalation_artifact["error"]
    assert "matched" in out["result"].escalation_artifact["error"], "the SPAN must survive"


def test_executed_failure_outranks_the_advisory_but_carries_it(monkeypatch):
    """Both fired. L5's verdict is the informative one, so it is returned -- but the advisory
    rides along instead of being dropped, or the retry loses why L2 objected."""
    _pin_l5(monkeypatch, ran=True, passed=False, summary="L5: 1 test(s) failed")
    out = subtask_graph.verify_subtask_node(_state(_ADV_OUTPUT))
    assert out["result"].status == "FAIL"
    assert out["verification_level"] == 5
    art = out["result"].escalation_artifact
    assert art["error"] == "L5: 1 test(s) failed"
    assert "l2_advisory" in art, "the L2 finding must not be silently discarded"


def test_control_no_refusal_still_passes(monkeypatch):
    """Control cell, in the same file as the claim: identical output minus the refusal phrase
    must pass. Without this, all three tests above could be green because L2 never fired."""
    _pin_l5(monkeypatch, ran=True, passed=True)
    out = subtask_graph.verify_subtask_node(_state(_CLEAN_OUTPUT))
    assert out["result"].status == "PASS"


def test_control_refusal_without_execution_is_not_a_blanket_fail(monkeypatch):
    """Second control: the clean output with NO execution must still pass, proving the
    level-2 return above is driven by the refusal and not merely by ran=False."""
    _pin_l5(monkeypatch, ran=False, passed=True)
    out = subtask_graph.verify_subtask_node(_state(_CLEAN_OUTPUT))
    assert out["result"].status == "PASS"
