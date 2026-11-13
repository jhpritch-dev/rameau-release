"""#156 -- a leg must never be told to call a function it is never given, AND the
independent fan-out must survive the repair.

Descriptions below are VERBATIM from the v354 bank (graph demobank-1):

    s1  "Implement csv_summary_stats(input_file) function that reads a CSV file and returns
         summary stats (count, mean, std, min, max) for numeric columns; handle
         missing/invalid data gracefully with defaults; include tests in same module."
    s2  "Implement main(argv) CLI entry point that calls csv_summary_stats and prints
         results in a readable format; supports --help flag showing usage; handles file not
         found and invalid CSV errors gracefully."

s2 declared dependencies=[] on all 173 leg executions across six banks and five builds, so
dep_context was 0 chars every time and s2's tests called a name that did not exist. It
failed on 1b, 2, 2.5 AND 2_qwen_coder while s1 passed on that same 2.5 -- the spec was
unsatisfiable, the model was not weak.

The decomposer prompt already required the opposite, and offered two repairs:
    "... MUST define every helper it calls within its OWN output, OR declare (in
     dependencies) the subtask ..."
Declaring the dependency is correct and SERIALIZES the fan-out. Operator constraint is to
preserve parallel fan-out at all costs, so the repair takes the self-containment branch.

THE LOAD-BEARING TEST HERE IS test_no_repair_ever_adds_a_dependency. If a future change
"fixes" a failure by adding an edge, it has traded the fan-out away and that must fail loudly.
"""
import pytest

from bifrost_message import SubtaskSpec
from autopilot_graph import _repair_undeclared_call_dependencies

S1_DESC = ("Implement csv_summary_stats(input_file) function that reads a CSV file and "
           "returns summary stats (count, mean, std, min, max) for numeric columns; handle "
           "missing/invalid data gracefully with defaults; include tests in same module.")
S2_DESC = ("Implement main(argv) CLI entry point that calls csv_summary_stats and prints "
           "results in a readable format; supports --help flag showing usage; handles file "
           "not found and invalid CSV errors gracefully.")
S3_DESC = ("Write concise CLI usage documentation for the tool, including how to run it "
           "(e.g., 'python csv_summary.py input.csv') and what it outputs.")


def _spec(sid, desc, deliverable="code_selftest", deps=None):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g-156", subtask_id=sid, description=desc,
        acceptance_criteria=["ac"], assigned_tier="1b", deliverable=deliverable,
        dependencies=list(deps or []),
    )


def _graph():
    return {
        "s1": _spec("s1", S1_DESC),
        "s2": _spec("s2", S2_DESC),
        "s3": _spec("s3", S3_DESC, deliverable="docs"),
    }


def test_no_repair_ever_adds_a_dependency():
    """THE constraint. Correctness must not be bought with the fan-out.

    Adding an edge would make s2 wait for s1. Independent parallel legs are a design
    requirement here, not an optimization -- if this fails, the repair has traded away the
    thing it was supposed to protect.
    """
    subs = _graph()
    _repair_undeclared_call_dependencies(subs)
    for sid, spec in subs.items():
        assert spec.dependencies == [], (
            "%s gained dependencies=%r -- the fan-out has been serialized"
            % (sid, spec.dependencies)
        )


def test_the_measured_csv_leg_is_told_to_define_what_it_calls():
    """THE case: s2 calls csv_summary_stats(), owned by s1, and was given nothing."""
    subs = _graph()
    repaired = _repair_undeclared_call_dependencies(subs)
    assert ("s2", "s1", "csv_summary_stats") in repaired, repaired
    d = subs["s2"].description
    assert "SELF-CONTAINED" in d
    assert "csv_summary_stats()" in d
    assert "IN PARALLEL" in d
    assert "do not" in d.lower()


def test_the_implementer_is_untouched():
    """s1 defines what it calls; nothing may be appended to it."""
    subs = _graph()
    _repair_undeclared_call_dependencies(subs)
    assert subs["s1"].description == S1_DESC


def test_docs_legs_are_untouched():
    subs = _graph()
    _repair_undeclared_call_dependencies(subs)
    assert subs["s3"].description == S3_DESC


def test_the_twofunc_shape_is_untouched():
    """Two genuinely independent legs -- the shape that already banks 5/6 clean.

    A repair that fires here would be inventing work and bloating the prompt.
    """
    subs = {
        "s1": _spec("s1", "Implement word_frequencies(text) AND its pytest suite in ONE "
                          "module; the tests call the function defined above."),
        "s2": _spec("s2", "Implement top_n_words(text, n) AND its pytest suite in ONE "
                          "module; the tests call the function defined above."),
    }
    before = {k: v.description for k, v in subs.items()}
    assert _repair_undeclared_call_dependencies(subs) == []
    assert {k: v.description for k, v in subs.items()} == before


def test_stdlib_furniture_does_not_trigger_a_repair():
    subs = {
        "s1": _spec("s1", "Implement open_report(path) that calls print() and open()."),
        "s2": _spec("s2", "Implement render(data) that calls print() and format()."),
    }
    assert _repair_undeclared_call_dependencies(subs) == []


def test_repair_is_idempotent():
    """Re-running must not append the directive twice -- prompt bytes are budgeted."""
    subs = _graph()
    _repair_undeclared_call_dependencies(subs)
    once = subs["s2"].description
    assert _repair_undeclared_call_dependencies(subs) == []
    assert subs["s2"].description == once


def test_the_added_sentence_is_small():
    """It rides in the leg description, charged against the envelope #152 guards."""
    subs = _graph()
    _repair_undeclared_call_dependencies(subs)
    grew = len(subs["s2"].description) - len(S2_DESC)
    assert 0 < grew < 260, "self-containment directive grew the description by %d" % grew


def test_empty_graph_is_safe():
    assert _repair_undeclared_call_dependencies({}) == []
