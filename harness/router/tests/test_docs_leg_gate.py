"""Docs-leg verification gate -- unit tests (2026-08-13).

Covers the defect measured in the v341 bank (RESULT-bank-3take-v341-2026-08-13.md): the s3
docs leg scored PASS in 3/3 takes while documenting functions that exist nowhere in the task,
because _l5_from_sandbox short-circuited every non-code leg to {"ran": False, "passed": True}
and L4 (semantic similarity) is explicitly advisory.

Two halves, and they are tested as a pair on purpose:
  PART B -- the gate (subtask_graph._l5_from_sandbox)
  PART A -- the decomposer exemplar (autopilot_graph._DECOMPOSER_SYSTEM), which is what makes
            the gate's B2 arm ABLE to fire. Part B alone can only report "NOT GATED".
"""
import json
import logging

import autopilot_graph as ag
import subtask_graph as sg
from bifrost_message import SubtaskSpec


def _docs_spec(desc, deps=None, dep_context=None):
    """A docs leg exactly as the decomposer emits one: placeholder function_name, no deps."""
    s = SubtaskSpec(
        type="subtask_spec", graph_id="demobank-1", subtask_id="s3", description=desc,
        acceptance_criteria=["accurate to the signatures"], assigned_tier="1a-hearth",
        dependencies=deps or [], function_name="_doc_section",
        input_signature="context: str", output_signature="str", deliverable="docs",
    )
    if dep_context is not None:
        s = s.model_copy(update={"dep_context": dep_context})
    return s


# The description the decomposer produces AFTER Part A: it names what to document.
_GOOD_DESC = (
    "Write concise usage documentation for these two functions, by these exact names: "
    "word_frequencies(text: str) -> dict[str,int], which counts how often each word occurs; "
    "and top_n_words(text: str, n: int) -> list[tuple[str,int]], which returns the n most "
    "frequent words with ties broken alphabetically."
)

# The description the decomposer produced BEFORE Part A -- it promises signatures and supplies
# none. Verbatim from the v341 bank.
_HOLLOW_DESC = "Concise usage documentation for the two functions (signatures supplied here)."


# ---------------------------------------------------------------------------
# PART B -- the gate
# ---------------------------------------------------------------------------

def test_docs_leg_documenting_the_right_functions_passes():
    out = ("## Usage\n\n`word_frequencies(text)` returns a dict mapping each lowercased word "
           "to the number of times it occurs in `text`. `top_n_words(text, n)` returns the n "
           "most frequent words as (word, count) pairs, with ties broken alphabetically.\n")
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["ran"] is True, "the gate must report that it actually gated"
    assert r["passed"] is True
    assert "word_frequencies" in r["summary"] and "top_n_words" in r["summary"]


def test_docs_leg_documenting_functions_that_do_not_exist_fails():
    """THE LIVE BUG. Reconstructed from the take-1 s3 output recorded in the v341 result:
    the leg documented compute_area and factorial while the task's functions were
    word_frequencies and top_n_words. This scored PASS three times running."""
    out = ("## API Documentation\n\n### compute_area(rectangle)\n\nReturns the area of the "
           "given rectangle.\n\n```python\ndef compute_area(rectangle):\n    return "
           "rectangle.w * rectangle.h\n```\n\n### factorial(n)\n\nReturns n!.\n\n"
           "```python\ndef factorial(n):\n    return 1 if n <= 1 else n * factorial(n - 1)\n```\n")
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["ran"] is True
    assert r["passed"] is False, "documenting entirely other functions must FAIL"
    assert "documents NONE of the identifiers" in r["summary"]
    assert "top_n_words" in r["summary"] and "word_frequencies" in r["summary"]


def test_docs_leg_that_emitted_code_instead_of_docs_fails():
    """v341 take 3: the docs leg emitted an implementation and no documentation at all."""
    out = "```python\ndef function_1(a, b):\n    return a + b\n```"
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["ran"] is True
    assert r["passed"] is False
    assert "emitted code, not documentation" in r["summary"]


def test_docs_leg_with_unterminated_fence_is_still_caught_as_code():
    """A truncated implementation must not slip past B1 by having its own body counted as
    prose because the closing fence never arrived."""
    out = "```python\ndef function_1(a, b):\n    return a + b\n"
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["passed"] is False and "emitted code, not documentation" in r["summary"]


def test_docs_leg_with_a_code_example_and_real_prose_passes():
    """B1 must not punish good documentation for containing a usage example."""
    out = ("`word_frequencies(text)` counts how often each word appears in the supplied text, "
           "returning a dictionary keyed by the lowercased word. Punctuation is ignored and "
           "the empty string yields an empty dict. A short example follows.\n\n"
           "```python\nword_frequencies('a b a')  # {'a': 2, 'b': 1}\n```\n")
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["passed"] is True, "prose + an example is documentation, not an implementation"


def test_docs_leg_with_no_expectations_is_NOT_GATED_and_warns(caplog):
    """The Part A hole. The spec named nothing to document, so the gate CANNOT judge the leg.
    It must say so -- and must NOT claim an L5 verification that never happened."""
    out = "Here is some documentation about compute_area and factorial, at length, in prose."
    with caplog.at_level(logging.WARNING):
        r = sg._l5_from_sandbox(object(), out, _docs_spec(_HOLLOW_DESC), False)
    assert r["ran"] is False, "ran=False keeps the leg at level 4 -- an honest 'unverified'"
    assert "NOT GATED" in r["summary"]
    # getMessage() does the %-substitution once. rec.message is ALREADY substituted, so
    # `rec.message % rec.args` formats a second time and raises TypeError.
    warned = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("NOT GATED" in rec.getMessage() for rec in warned), (
        "the hole must be LOUD, not silent")
    assert any("Part A" in rec.getMessage() for rec in warned), (
        "the warning must point at the root cause, not just report a symptom")


def test_partial_overlap_passes_deliberately():
    """Gate on ZERO overlap, not all-must-appear: a stricter rule would false-fail
    legitimate partial documentation. Total substitution is the measured defect."""
    out = ("`word_frequencies(text)` counts how often each word occurs in the given text and "
           "returns them in a dictionary keyed by the lowercased word form.")
    r = sg._l5_from_sandbox(object(), out, _docs_spec(_GOOD_DESC), False)
    assert r["ran"] is True and r["passed"] is True
    assert "1/2" in r["summary"]


def test_code_leg_path_is_untouched():
    """Regression guard: the docs gate must never intercept a code leg."""
    spec = SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s1", description="Implement it",
        acceptance_criteria=[], assigned_tier="1b", dependencies=[],
        function_name="word_frequencies", input_signature="text: str",
        output_signature="dict[str,int]", deliverable="code_selftest",
    )

    class _R:
        escalation_artifact = {}

    r = sg._l5_from_sandbox(_R(), "```python\ndef word_frequencies(t): return {}\n```", spec, True)
    assert "L5-docs" not in r.get("summary", ""), "docs gate leaked onto a code leg"


# ---------------------------------------------------------------------------
# _docs_expected_identifiers -- the input side of B2
# ---------------------------------------------------------------------------

def test_expected_identifiers_read_from_description():
    assert sg._docs_expected_identifiers(_docs_spec(_GOOD_DESC)) == {
        "word_frequencies", "top_n_words"}


def test_expected_identifiers_ignores_ordinary_prose():
    """`\\s*` before the paren would make 'functions' an expected identifier out of the phrase
    'the two functions (signatures supplied here)', manufacturing a false failure."""
    assert sg._docs_expected_identifiers(_docs_spec(_HOLLOW_DESC)) == set()


def test_expected_identifiers_skips_builtins_and_privates():
    spec = _docs_spec("document parse_csv(path: str) -> list[dict] and _helper(x); uses str()")
    assert sg._docs_expected_identifiers(spec) == {"parse_csv"}


def test_expected_identifiers_read_from_dep_context_too():
    spec = _docs_spec("document the API", dep_context="```python\ndef main(argv): ...\n```")
    assert "main" in sg._docs_expected_identifiers(spec)


# ---------------------------------------------------------------------------
# PART A -- the decomposer exemplar, and the fact that it ACTIVATES the gate
# ---------------------------------------------------------------------------

def _exemplar_subtasks(system_prompt):
    i = system_prompt.index('{"subtasks":')
    return json.loads(system_prompt[i:].strip())["subtasks"]


def test_decomposer_exemplar_docs_leg_names_what_it_asks_to_be_documented():
    """The root cause. The old exemplar's description promised '(signatures supplied here)'
    and supplied nothing, so the model copied a template for documenting functions it was
    never shown -- and invented plausible ones instead."""
    s3 = [s for s in _exemplar_subtasks(ag._DECOMPOSER_SYSTEM)
          if s["deliverable"] == "docs"][0]
    desc = s3["description"]
    assert "word_frequencies" in desc and "top_n_words" in desc, (
        "the docs exemplar must NAME the functions it asks to be documented")
    assert "signatures supplied here" not in desc, (
        "the exemplar must not promise signatures it does not supply")


def test_decomposer_exemplar_docs_leg_stays_parallel():
    """Naming the signatures is the ALTERNATIVE to a dependency. If Part A ever regresses into
    adding deps, the docs leg leaves wave 1 and bank timings change -- catch that here."""
    s3 = [s for s in _exemplar_subtasks(ag._DECOMPOSER_SYSTEM)
          if s["deliverable"] == "docs"][0]
    assert s3["dependencies"] == [], "a docs leg must stay dependency-free to run in parallel"
    assert s3["assigned_tier"] == "1a-hearth"


def test_part_A_makes_part_B_gateable():
    """THE INTEGRATION POINT. Part B's B2 arm can only fire when the spec names identifiers.
    Build a spec from the SHIPPING exemplar description and prove the gate is now live --
    i.e. that the 'NOT GATED' warning has actually been retired for this shape."""
    s3 = [s for s in _exemplar_subtasks(ag._DECOMPOSER_SYSTEM)
          if s["deliverable"] == "docs"][0]
    spec = _docs_spec(s3["description"])
    expected = sg._docs_expected_identifiers(spec)
    assert expected, "Part A failed: the exemplar still names nothing, so B2 cannot gate"

    bad = ("## compute_area(rectangle)\n\nReturns the area of the rectangle supplied to it, "
           "computed as width multiplied by height, in square units.")
    r = sg._l5_from_sandbox(object(), bad, spec, False)
    assert r["ran"] is True and r["passed"] is False, (
        "with Part A landed, the v341 substitution failure must now FAIL for real")
