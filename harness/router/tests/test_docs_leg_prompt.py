"""#138 fix B -- the docs leg must be told only things that are TRUE of a prose deliverable.

A docs leg is emitted with dependencies=[] and runs in the SAME wave as the coding legs
(autopilot_graph.py:947,951), so no implementation exists when it runs. It is handed SIGNATURES,
and a signature carries a return TYPE, not a return VALUE -- so a worked example's output cannot
be derived, only invented. _build_messages therefore gives docs legs their own contract.

The branch is THREE-way (docs / tests / implementation) and these tests pin all three arms plus
the IMPORT SAFETY block, which is scoped separately because it precedes the branch. The negative
arms are the ones that matter, and each corresponds to a defect that was live and silent:

  * while the branch was binary, a docs leg received the IMPLEMENTATION contract -- including
    "Do NOT write usage examples"
  * the first cut of the fix left a bare `else`, handing it the TEST contract instead
  * IMPORT SAFETY told it "your output is compiled AND imported by an automated verifier", which
    is false: _CODE_DELIVERABLES (subtask_graph.py:932) excludes "docs" and only code legs are
    POSTed to execsvc (:1651), so a docs leg is never compiled and never imported
"""
import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages

DOCS_BLOCK = "YOU HAVE NOT SEEN THE IMPLEMENTATION"
IMPL_BLOCK = "DELIVER THE IMPLEMENTATION ONLY"
TEST_BLOCK = "HOW YOUR TESTS ARE RUN"
# #145: code_selftest is a FOURTH arm. Until it had one it fell into IMPL_BLOCK and was told not
# to write the tests its deliverable is defined as containing. See test_selftest_leg_prompt.py.
SELFTEST_BLOCK = "DELIVER IMPLEMENTATION + ITS OWN TESTS"
IMPORT_BLOCK = "IMPORT SAFETY"


def _spec(deliverable, function_name):
    return SubtaskSpec(
        type="subtask_spec",
        graph_id="g-138b",
        subtask_id="s1",
        description="Document word_frequencies(text: str) -> dict[str, int].",
        acceptance_criteria=["ac"],
        assigned_tier="1a-hearth" if deliverable == "docs" else "2_qwen_coder",
        complexity_band="MODERATE",
        dependencies=[],
        context_budget_tokens=4000,
        think=False,
        function_name=function_name,
        input_signature="text: str",
        output_signature="dict[str,int]",
        deliverable=deliverable,
    )


def _system(deliverable, function_name):
    return _build_messages(_spec(deliverable, function_name), [])[0]["content"]


@pytest.mark.parametrize(
    "deliverable,function_name,docs,impl,selftest,tests,imports",
    [
        ("docs", "_doc_section", True, False, False, False, False),
        ("code_selftest", "word_frequencies", False, False, True, False, True),
        ("code", "word_frequencies", False, True, False, False, True),
        ("tests", "test_wf", False, False, False, True, True),
    ],
)
def test_leg_kind_selects_exactly_one_contract(
    deliverable, function_name, docs, impl, selftest, tests, imports
):
    """#145: the code_selftest row read (impl=True) until 2026-08-15, which pinned a live defect
    as correct -- that arm forbids the very tests a code_selftest leg is FAILED for omitting.
    Enumerating the arms is not the same as checking that each says something true of its
    deliverable, which is the lesson the docs leg taught earlier the same day."""
    s = _system(deliverable, function_name)
    assert (DOCS_BLOCK in s) is docs
    assert (IMPL_BLOCK in s) is impl, "a docs leg must not be told to suppress usage examples"
    assert (SELFTEST_BLOCK in s) is selftest, "only a code_selftest leg owes its own tests"
    assert (TEST_BLOCK in s) is tests, "a docs leg must not be given the test-execution contract"
    assert (IMPORT_BLOCK in s) is imports, "a docs leg is never compiled or imported"


def test_docs_prompt_asserts_nothing_about_a_module():
    """Each of these was reaching the docs leg as a 'hard requirement' and is false of prose."""
    s = _system("docs", "_doc_section")
    for claim in (
        "your output is compiled AND imported",
        "The module MUST be safe to import",
        "Define functions, classes and constants at module scope",
        'if __name__ == "__main__"',
        "A module that raises on import is REJECTED",
    ):
        assert claim not in s, claim


def test_docs_contract_names_the_shapes_it_forbids():
    """A constraint that does not name the artifact is decoration; these are what got emitted."""
    s = _system("docs", "_doc_section")
    for token in ("# Output:", ">>>", "return TYPE, never a return VALUE"):
        assert token in s


def test_docs_contract_still_permits_what_the_L5_gate_requires():
    """The L5 docs gate wants prose and the expected identifiers -- the call SHAPE carries both."""
    s = _system("docs", "_doc_section")
    assert "call SHAPE" in s
    assert "do not write prose" not in s.lower()


def test_docs_classification_is_field_only():
    """No keyword fallback: a false positive would strip a CODE leg of its implementation
    contract AND its import-safety contract, and 'document' appears routinely in code-leg
    descriptions."""
    spec = _spec("code_selftest", "word_frequencies")
    spec.description = "Write documentation-quality code that explains and documents itself."
    s = _build_messages(spec, [])[0]["content"]
    assert DOCS_BLOCK not in s
    assert SELFTEST_BLOCK in s   # #145: this spec is code_selftest, so this is its arm
    assert IMPORT_BLOCK in s
