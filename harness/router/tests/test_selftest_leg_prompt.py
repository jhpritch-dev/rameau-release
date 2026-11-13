"""#145 -- a code_selftest leg was forbidden to write the tests it is graded on.

Measured, not inferred: _build_messages returned a BYTE-IDENTICAL system prompt for deliverable
"code" and "code_selftest" (2062 chars each). `_is_test_leg` matched only "tests", so
code_selftest fell into the implementation arm and was told:

    "- Do NOT write test functions, pytest cases, bare `assert` checks, or a test module."

while _TEST_BEARING_DELIVERABLES (subtask_graph.py:955) includes code_selftest and #45 made a
code_selftest leg that collects zero tests a hard FAIL. Across 12 csv coding-leg outputs from two
builds: `def test_` 1/12, argparse 9/12, __main__ 11/12, open( 11/12. The legs obeyed.

test_docs_leg_prompt.py::test_leg_kind_selects_exactly_one_contract asserted this defect was
correct behavior -- it pinned code_selftest to the implementation contract. Enumerating the arms
is not the same as checking each arm says something true of its deliverable, which is the same
lesson the docs leg taught earlier the same day.
"""
import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages

FORBIDS_TESTS = "Do NOT write test functions"
REQUIRES_TESTS = "DELIVER IMPLEMENTATION + ITS OWN TESTS"
IMPL_BLOCK = "DELIVER THE IMPLEMENTATION ONLY"
FILE_BLOCK = "TESTING A FILE / CLI TASK"

CSV_DESC = ("Write a command-line tool that reads a CSV file and prints summary statistics "
            "for each numeric column")
MATH_DESC = "Compute the nth Fibonacci number iteratively"


def _spec(deliverable, description=CSV_DESC, function_name="csv_summary_stats"):
    return SubtaskSpec(
        type="subtask_spec",
        graph_id="g-145",
        subtask_id="s1",
        description=description,
        acceptance_criteria=["ac"],
        assigned_tier="2_qwen_coder",
        complexity_band="MODERATE",
        dependencies=[],
        context_budget_tokens=4000,
        think=False,
        function_name=function_name,
        input_signature="path: str",
        output_signature="dict[str, dict[str, float]]",
        deliverable=deliverable,
    )


def _system(deliverable, description=CSV_DESC, function_name="csv_summary_stats"):
    return _build_messages(_spec(deliverable, description, function_name), [])[0]["content"]


def test_a_selftest_leg_is_not_told_to_omit_tests():
    """The defect itself, stated as its own test."""
    assert FORBIDS_TESTS not in _system("code_selftest")


def test_a_selftest_leg_is_told_it_owes_tests():
    s = _system("code_selftest")
    assert REQUIRES_TESTS in s
    assert "collects ZERO tests is REJECTED" in s
    assert "def test_<name>()" in s, "must name the shape pytest actually collects"


def test_code_and_selftest_prompts_are_not_identical():
    """The regression in the form it was measured: the two prompts were byte-for-byte the same,
    so nothing in the prompt distinguished a leg that owes tests from one that must not write
    them. Length alone is a weak assertion; the two above say what changed. This one says the
    kinds are distinguishable at all, which is what silently stopped being true."""
    assert _system("code") != _system("code_selftest")


def test_a_plain_code_leg_is_still_told_to_omit_tests():
    """The other side of the split. A plain `code` leg's volunteered tests run against its own
    unintegrated fragment and REJECT the leg if they fail -- that contract is unchanged and this
    fix must not have widened the hole in the other direction."""
    s = _system("code")
    assert FORBIDS_TESTS in s
    assert IMPL_BLOCK in s
    assert REQUIRES_TESTS not in s


def test_a_selftest_leg_keeps_the_contracts_it_shares_with_code():
    """SIGNATURE CONTRACT and STDLIB ONLY are nested inside the shared arm. Losing STDLIB ONLY
    would let pandas into a CSV task, which is a ModuleNotFoundError in the air-gapped verifier --
    a silent regression that the tests-related assertions above would not have caught."""
    s = _system("code_selftest")
    assert "IMPORT SAFETY" in s
    assert "STDLIB ONLY" in s
    assert "SIGNATURE CONTRACT" in s
    assert "def csv_summary_stats(path: str) -> dict[str, dict[str, float]]:" in s


@pytest.mark.parametrize("deliverable", ["code", "tests", "docs"])
def test_the_other_three_kinds_are_untouched(deliverable):
    """Guards the blast radius: only the code_selftest arm was meant to move."""
    s = _system(deliverable, function_name="csv_summary_stats" if deliverable != "docs" else "_doc")
    assert REQUIRES_TESTS not in s
    assert FILE_BLOCK not in s


def test_the_file_task_block_reaches_a_csv_selftest_leg():
    s = _system("code_selftest")
    assert FILE_BLOCK in s
    assert "io.StringIO" in s and "tmp_path" in s


def test_the_file_task_block_does_not_fire_on_a_pure_computation():
    """Narrow by design -- it is advice for one measured failure mode, not a standing block."""
    assert FILE_BLOCK not in _system("code_selftest", MATH_DESC, "fib")


def test_the_file_task_block_resolves_the_import_safety_contradiction():
    """IMPORT SAFETY says 'Importing it must not read files'. A leg writing a file tool can read
    that as 'I cannot exercise file reading here at all' and write nothing. Whether that IS a
    cause is unmeasured -- the probe was never run -- but the ambiguity is removable in one
    sentence, so both blocks must state which of the two moments they govern."""
    s = _system("code_selftest")
    assert "Importing it must not read files" in s
    assert "WHEN THE MODULE IS IMPORTED" in s
    assert "pytest RUNS it" in s


def test_selftest_classification_is_field_only():
    """No keyword fallback, for the same reason _is_docs_leg has none: a false positive would
    tell a plain `code` leg to volunteer tests, and volunteered tests are run against an
    unintegrated fragment and REJECT the leg when they fail."""
    spec = _spec("code")
    spec.description = "Write a self-testing CSV parser with a full pytest suite and selftest"
    s = _build_messages(spec, [])[0]["content"]
    assert REQUIRES_TESTS not in s
    assert FORBIDS_TESTS in s
