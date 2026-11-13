"""
Guards for test-deliverable enforcement -- the host computes what it can prove.

WHY THIS IS CODE AND NOT GRAMMAR. minItems works because a count is a STRUCTURAL property of
an array. "The test subtask must depend on the implementation subtask" is SEMANTIC (which leg
is the test?) and CROSS-FIELD (it must contain another leg's MODEL-CHOSEN id). No JSON Schema
construct llama.cpp compiles can express it, so the model can only be ASKED -- and measured,
asking is unreliable:

  - v226: the decomposer DID declare the dependency (wave 1 = [s1, s2], s3 held back)
  - v227: same prompt, same model, NO dependency (wave 1 = [s1, s2, s3])
  - and the live decomposition drops the test leg entirely: sum_numeric_column +
    _doc_section + _analysis_section, for a prompt that says "with a unit test"

Same MoE variance as 3-vs-1 subtasks at temperature 0 (why `legs` exists). So enforcement is
post-parse, mirroring _has_dag_cycle: validate the parsed decomposition, repair what is
derivable, and return FAILED -> REPLAN for what is not.
"""
import pytest

from autopilot_graph import (
    _WANTS_TEST,
    _enforce_test_deliverable,
    _is_impl_leg,
    _is_test_leg,
)
from bifrost_message import SubtaskSpec

DEMO = "Write a Python function to parse a CSV file and return the sum of a numeric column, with a unit test."
PROSE = "Summarize the tradeoffs of local LLM inference vs cloud APIs."


def _sp(sid, fn, desc="d", deps=None, sig="p: str", ret="float"):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id=sid, description=desc,
        acceptance_criteria=[], assigned_tier="1b", dependencies=deps or [],
        function_name=fn, input_signature=sig, output_signature=ret,
    )


# --- leg identification (the function_name convention already in the data) -----------

def test_impl_leg_identified_by_real_function_name():
    assert _is_impl_leg(_sp("s1", "sum_numeric_column"))


@pytest.mark.parametrize("fn", ["_doc_section", "_analysis_section"])
def test_underscore_legs_are_not_impl(fn):
    """"_doc_section" is the spec builder's own default; the model invents siblings."""
    assert not _is_impl_leg(_sp("s2", fn))


def test_test_leg_is_not_an_impl_leg():
    assert not _is_impl_leg(_sp("s2", "test_sum"))
    assert _is_test_leg(_sp("s2", "test_sum"))


# --- _WANTS_TEST must be NARROW -----------------------------------------------------

@pytest.mark.parametrize("prompt", [
    DEMO,
    "Add pytest coverage.",
    "Write a parser and a unit test.",
])
def test_prompts_that_ask_for_a_test(prompt):
    assert _WANTS_TEST.search(prompt)


@pytest.mark.parametrize("prompt", [
    PROSE,
    "Write a whitepaper about testing strategy.",   # a bare 'test' must NOT fire
    "Implement a binary search in Python.",
])
def test_prompts_that_do_not_ask_for_a_test(prompt):
    """Too broad and synthesis fires on prose runs, serializing and padding the document
    path -- which currently works (PROSE = COMPLETE 3/3). This is the sharp edge."""
    assert not _WANTS_TEST.search(prompt)


# --- the no-op safety property ------------------------------------------------------

def test_prose_decomposition_is_untouched():
    st = {"s1": _sp("s1", "_doc_section"), "s2": _sp("s2", "_analysis_section")}
    before = {k: v.model_dump() for k, v in st.items()}
    assert _enforce_test_deliverable(st, PROSE) is None
    assert {k: v.model_dump() for k, v in st.items()} == before


def test_existing_dependency_is_never_overwritten():
    st = {"s1": _sp("s1", "parse_csv_sum"), "s2": _sp("s2", "test_parse_csv_sum", deps=["s9"])}
    _enforce_test_deliverable(st, DEMO)
    assert st["s2"].dependencies == ["s9"]


# --- derive -------------------------------------------------------------------------

def test_derives_test_to_impl_from_function_name():
    """test_X -> the leg whose function_name is X. Derivable from data the decomposer
    already emits, so it costs nothing and cannot vary run to run."""
    st = {"s1": _sp("s1", "parse_csv_sum"), "s2": _sp("s2", "test_parse_csv_sum")}
    assert _enforce_test_deliverable(st, DEMO) is None
    assert st["s2"].dependencies == ["s1"]


def test_derives_against_the_sole_impl_when_names_do_not_match():
    st = {"s1": _sp("s1", "sum_csv_column"), "s2": _sp("s2", "test_something_else")}
    _enforce_test_deliverable(st, DEMO)
    assert st["s2"].dependencies == ["s1"]


def test_derive_does_not_synthesize_an_extra_leg():
    st = {"s1": _sp("s1", "parse_csv_sum"), "s2": _sp("s2", "test_parse_csv_sum")}
    _enforce_test_deliverable(st, DEMO)
    assert len(st) == 2


# --- synthesize ---------------------------------------------------------------------

def _synth(st=None):
    st = st or {
        "s1": _sp("s1", "sum_numeric_column", sig="csv_path: str, column_name: str"),
        "s2": _sp("s2", "_doc_section"),
        "s3": _sp("s3", "_analysis_section"),
    }
    assert _enforce_test_deliverable(st, DEMO) is None
    return st, next(s for s in st.values() if (s.function_name or "").startswith("test_"))


def test_synthesizes_the_missing_test_leg():
    """THE case measured live: prompt says 'with a unit test', decomposer produced
    impl + _doc_section + _analysis_section and no test at all."""
    st, test = _synth()
    assert len(st) == 4
    assert test.function_name == "test_sum_numeric_column"


def test_synthesized_leg_carries_the_dependency_preset():
    _, test = _synth()
    assert test.dependencies == ["s1"], "the dependency must be impossible to forget"


def test_synthesized_description_is_self_contained():
    """It embeds the signature, so the leg works even if dep_context never arrives."""
    _, test = _synth()
    assert "sum_numeric_column(csv_path: str, column_name: str) -> float" in test.description


def test_synthesized_description_demands_a_runnable_module():
    """_verify_level5 runs pytest on THIS leg's output ALONE (subtask_graph.py:807):

        proc = _sub.run(["python", "-m", "pytest", tmp_path, ...])
        passed = proc.returncode in (0, 5)   # 5 = no tests collected -> pass

    A leg emitting ONLY tests NameErrors on the implementation -> pytest fails -> the
    blocking gate fails the very leg enforcement just added. So the description must demand
    implementation AND tests in one runnable block.
    """
    _, test = _synth()
    d = test.description.lower()
    assert "runs standalone" in d
    assert "both" in d and "implementation" in d


def test_synthesized_leg_uses_a_real_fleet_tier():
    from autopilot_graph import _DECOMPOSE_TIERS
    _, test = _synth()
    assert test.assigned_tier in _DECOMPOSE_TIERS


def test_synthesized_id_does_not_collide():
    st, test = _synth()
    assert test.subtask_id == "s4"
    assert len([s for s in st.values() if s.subtask_id == "s4"]) == 1


# --- unrepairable -> FAILED -> REPLAN (the _has_dag_cycle pattern) -------------------

def test_no_impl_to_test_returns_failed_not_a_bogus_leg():
    st = {"s1": _sp("s1", "_doc_section"), "s2": _sp("s2", "_analysis_section")}
    r = _enforce_test_deliverable(st, DEMO)
    assert r is not None and r["status"] == "FAILED"
    assert "no implementation subtask" in r["error"]
    assert len(st) == 2, "must not invent a test with nothing to test"


# --- the wiring that would make all of this silently inert ---------------------------

def test_dag_edges_are_built_from_specs_not_raw_subtasks():
    """Enforcement adds dependencies (and whole subtasks) that never existed in the
    decomposer's raw output. A reverse-edge build reading raw_subtasks would not see them:
    fan_out would never mark the dependent blocked and dep_count would be wrong -- the
    enforcement would be silently inert.
    """
    import ast
    import inspect
    import autopilot_graph
    src = inspect.getsource(autopilot_graph._decompose_async)
    tree = ast.parse(src.strip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            tgt = getattr(node.targets[0], "id", "")
            if tgt == "dag_edges" and isinstance(node.value, ast.DictComp):
                return  # rebuilt from a comprehension over subtasks
    seg = src
    assert "dag_edges = {sid: [] for sid in subtasks}" in seg, (
        "dag_edges must be rebuilt from the specs AFTER enforcement"
    )


def test_dep_context_instruction_does_not_tell_a_test_leg_to_omit_the_code():
    """A dependent leg must never be verified against an INCOMPLETE program.

    ORIGINAL invariant (pre-2026-07-23): L5 executed each leg's output ALONE, so the prompt
    had to order a verbatim copy ("COPY this code into your own output") -- telling a test leg
    to omit the implementation guaranteed a NameError and an L5 failure.

    That workaround was itself the dominant failure mode: measured over the v241 10-take bank,
    the s3 pytest leg passed 1/10 because the model transcribed the dependency INCOMPLETELY
    (dropping `from typing import Dict, Any`, helper classes, whole functions) ->
    "module FAILED to run: module raised on import".

    The fix moved the guarantee from the PROMPT to the HARNESS: execute_subtask_node now
    prepends the dependency's code to the L5 sandbox payload, so a dependent leg is executed
    as a COMPLETE program (matching what L6 already does with the assembled output). The leg
    therefore no longer needs to transcribe anything.

    So this test now asserts the STRONGER, mechanical property -- the dep code actually
    reaches the verifier -- instead of asserting the wording of a prompt workaround.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "subtask_graph.py").read_text(encoding="utf-8")

    # The L5 payload must NOT be the bare leg output any more.
    assert '"output": _l5_output' in src, (
        "L5 must POST the dep-inclusive payload, not the leg's bare `text`"
    )
    # The dependency code must be extracted from dep_context and prepended. (v264: extraction
    # is now parse-filtered via _parseable_py_blocks -- same _FENCE_PY_RE underneath, plus a
    # drop of any leaked non-Python block that would syntax-fail the merged module.)
    assert "_parseable_py_blocks(spec.dep_context)" in src, (
        "dependency code must be extracted from dep_context for the L5 payload"
    )
    # The leg's OWN code must be extracted too, and merged with the dep code into a SINGLE
    # fence. Regression guard for the v242 bug: emitting the dep code as its own ```python
    # block ahead of the leg's block meant the extractor took only the FIRST block -- the
    # sandbox got the implementation and never saw the tests (tests=0/0/0), then ran the
    # implementation's CLI entrypoint bare and SystemExited ("module raised on import").
    assert "_FENCE_PY_RE.findall(text" in src, (
        "the leg's own fenced code must be extracted so it can be merged, not appended raw"
    )
    assert '_l5_output = "```python\\n" + _dep_code' in src, (
        "dep code and leg code must be merged INSIDE ONE fence -- two fences means the "
        "extractor takes only the first block and the leg's own code is silently dropped"
    )
    # A dependency's `if __name__ == '__main__':` entrypoint must be stripped: the sandbox
    # EXECUTES the module, so a bare main() with required args SystemExits before pytest runs.
    assert "__main__" in src and "_dep_code = _re.sub(" in src, (
        "dependency CLI entrypoint must be stripped from the L5 payload"
    )

    # And the prompt must still forbid RE-DERIVING the dependency (the half that stays true).
    assert "do not re-derive" in src
