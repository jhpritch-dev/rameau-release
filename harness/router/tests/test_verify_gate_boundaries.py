"""Boundaries for three verification gates that reported things that were not true.

#45  a code_selftest leg that collected ZERO tests scored PASS
#51  the L2 refusal gate matched any word ending in "i" before " cannot", and the PREFIX of
     "as an aid" / "as an AIX" / "as an airgapped"
#54  every failing L5 summary was emitted as "L5: L5: ..."

Each case below was reproduced against the LIVE gate in the v328 bank pod before the fix; these
tests pin the behavior so it cannot silently return.
"""
import pytest

import subtask_graph
from bifrost_message import SubtaskResult, compute_checksum

DESC = "Implement a function that adds two numbers"
CODE = "```python\ndef f(): return 2\ndef test_f(): assert f() == 2\n```"

_NO_TESTS = {
    "passed": True, "syntax_ok": True, "executed": True, "returncode": 5,
    "summary": "compiles + runs clean; NO TESTS PRESENT",
    "stdout": "\nno tests ran in 0.00s\n", "stderr": "",
    "tests_collected": 0, "tests_run": 0, "tests_failed": 0, "tests_detail": [],
}
_ONE_TEST = dict(_NO_TESTS, returncode=0, tests_collected=1, tests_run=1,
                 summary="compiles + runs clean; 1 test(s) passed")


class _Spec:
    def __init__(self, deliverable=None, description=DESC):
        self.description = description
        self.subtask_id = "s1"
        self.graph_id = "g1"
        if deliverable is not None:
            self.deliverable = deliverable


def _gate(sandbox_l5, deliverable=None, output=CODE):
    spec = _Spec(deliverable)
    res = SubtaskResult(
        type="subtask_result", subtask_id="s1", graph_id="g1", status="PASS",
        output=output, output_checksum=compute_checksum(output),
        verification_level_reached=0, tier_used="1b", tokens_used=1,
        escalation_artifact={"sandbox_l5": sandbox_l5},
    )
    return subtask_graph._l5_from_sandbox(
        res, output, spec, subtask_graph._CODE_TASK_RE.search(DESC) is not None)


# --- #45: zero collected tests from a test-BEARING leg is a MISSING deliverable ---------------

@pytest.mark.parametrize("deliverable", ["code_selftest", "tests"])
def test_test_bearing_leg_with_zero_tests_is_FAILED(deliverable):
    """The defect: `== "tests"` let code_selftest through. Its whole contract is impl AND
    tests in one module, so zero collected means the deliverable is absent."""
    out = _gate(_NO_TESTS, deliverable=deliverable)
    assert out["passed"] is False
    assert out["ran"] is True, "must claim the gate GATED, or the leg reports level 4 and passes"
    assert "collected 0 tests" in out["summary"]
    assert deliverable in out["summary"], "the message must name the actual leg kind"


def test_plain_code_leg_with_zero_tests_still_passes_uncredited():
    """The load-bearing counterpart. An implementation-only leg owes no tests; failing it here
    would fail every impl leg in the graph. Passes, but ran=False so it does NOT claim L5."""
    out = _gate(_NO_TESTS, deliverable="code")
    assert out["passed"] is True
    assert out["ran"] is False


def test_code_selftest_with_real_tests_still_passes_at_level_5():
    out = _gate(_ONE_TEST, deliverable="code_selftest")
    assert out["passed"] is True and out["ran"] is True


def test_plain_code_is_not_in_the_test_bearing_set():
    """Guards the distinction directly: merging these two sets is the tempting wrong fix."""
    assert "code" not in subtask_graph._TEST_BEARING_DELIVERABLES
    assert subtask_graph._TEST_BEARING_DELIVERABLES < subtask_graph._CODE_DELIVERABLES


# --- #51: a refusal is first-person, in prose, near the top -----------------------------------

BENIGN = [
    "The API cannot accept a payload larger than 4MB, so callers must chunk it.",
    "ASCII cannot represent the em dash, so we normalize it to a hyphen.",
    "The CLI cannot run without a TTY; use the batch entrypoint instead.",
    "WiFi cannot sustain that bitrate; fall back to the wired path.",
    "SCSI cannot address more than 16 targets on this bus.",
    "Hawaii cannot be reached by the terrestrial fiber link.",
    "Treat the token bucket as an aid to smoothing burst traffic.",
    "We ship it as an AIX package for the legacy host.",
    "Deploy as an airgapped bundle; there is no egress.",
]

REFUSALS = [
    "I cannot help with that request.",
    "As an AI, I am unable to comply.",
    "I'm unable to write that code.",
    "I can't do that.",
    "I will not produce that.",
]


@pytest.mark.parametrize("text", BENIGN)
def test_technical_prose_is_not_a_refusal(text):
    """Every one of these hard-FAILED at L2 before the fix -- measured, not hypothesized."""
    assert subtask_graph._REFUSAL_RE.search(text) is None, text


@pytest.mark.parametrize("text", REFUSALS)
def test_real_refusals_are_still_caught(text):
    """Widening the boundaries must not blunt detection. The last two are NEW coverage the
    old substring list missed entirely."""
    assert subtask_graph._REFUSAL_RE.search(text) is not None, text


def test_a_refusal_phrase_inside_delivered_code_is_not_a_refusal():
    """L2 runs BEFORE L5, so a false positive here kills code the sandbox would have passed.
    A docstring is not the model refusing."""
    artifact = (
        "```python\n"
        'def upload(payload):\n'
        '    """The API cannot accept more than 4MB, so we split."""\n'
        "    return payload\n"
        "```"
    )
    prose = subtask_graph._REFUSAL_FENCE_RE.sub(" ", artifact)
    assert subtask_graph._REFUSAL_RE.search(prose[:subtask_graph._REFUSAL_WINDOW]) is None


def test_refusal_detection_looks_at_the_head_not_the_whole_document():
    """A refusal is a preamble emitted INSTEAD of the artifact, not a remark 8kB in."""
    buried = ("x" * (subtask_graph._REFUSAL_WINDOW + 200)) + " I cannot continue."
    assert subtask_graph._REFUSAL_RE.search(buried[:subtask_graph._REFUSAL_WINDOW]) is None
    assert subtask_graph._REFUSAL_RE.search("I cannot continue. " + buried) is not None


# --- #54: the prefix is added by the producer, not the consumer -------------------------------

def test_the_producer_prefixes_exactly_once():
    out = _gate(_NO_TESTS, deliverable="tests")
    assert out["summary"].startswith("L5: ")
    assert not out["summary"].startswith("L5: L5:")


def test_the_consumer_does_not_re_prefix():
    """The doubling came from verify_subtask_node wrapping an already-prefixed summary.
    Asserted against the source because the consumer sits deep inside a node function that
    needs a full graph state to invoke -- and the invariant being protected is textual:
    exactly one side owns the prefix, and it is the producer."""
    import inspect
    src = inspect.getsource(subtask_graph.verify_subtask_node)
    assert "\"error\": f\"L5: {l5_result['summary']}\"" not in src, (
        "the consumer re-prefixed a summary that already starts with 'L5: ' (#54)")
