"""L5 applicability must be decided by the DELIVERABLE, not by how a leg was phrased.

WHY. Every consumer used to re-derive "is this a code leg?" from free text with its own
regex, and they disagreed. Measured live 2026-07-23 across a 39-take soak:

  * an s4 test leg reported PASS while its sandbox verdict was passed=False -- "Provide a
    pytest test suite..." missed _CODE_TASK_RE, so L5 was skipped and a real failure was
    never counted;
  * two other s4 legs, same job, different phrasing, DID match -- and their identical
    failure failed the take.

A gate that fires on phrasing is a coin flip in BOTH directions: it lets bad work through
as readily as it fails good work. So the kind is now resolved once, host-side, in
decompose_node and carried on the spec.

These tests pin three things: the resolution order, that the field drives L5, and that a
graph from an older build (no field) still works on the legacy path.
"""
import pytest

from autopilot_graph import _DELIVERABLE_KINDS, _resolve_deliverable, _decompose_schema
from bifrost_message import SubtaskSpec
from subtask_graph import _CODE_TASK_RE, _DOCS_TASK_RE


def _spec(**kw):
    base = dict(
        type="subtask_spec", graph_id="g", subtask_id="s1",
        description="do a thing", acceptance_criteria=[], assigned_tier="1b",
    )
    base.update(kw)
    return SubtaskSpec(**base)


# --- resolution order -----------------------------------------------------------------

def test_function_name_convention_outranks_the_model():
    """The host proves it from structure; a model claim cannot override that."""
    s = _spec(function_name="test_parse_csv", deliverable="code")
    assert _resolve_deliverable(s) == "tests"


def test_underscore_prefix_is_docs_even_if_model_said_code():
    s = _spec(function_name="_doc_section", deliverable="code")
    assert _resolve_deliverable(s) == "docs"


def test_model_value_used_when_structure_is_silent():
    s = _spec(function_name="parse_csv", deliverable="docs")
    assert _resolve_deliverable(s) == "docs"


def test_illegal_model_value_falls_through_to_prose():
    s = _spec(function_name="parse_csv", deliverable="banana",
              description="Write user documentation for the CLI tool.")
    assert _resolve_deliverable(s) == "docs"


def test_prose_fallback_when_nothing_else_available():
    assert _resolve_deliverable(
        _spec(description="Write pytest unit tests for parse_csv.")) == "tests"
    assert _resolve_deliverable(
        _spec(description="Write user documentation explaining the CLI.")) == "docs"
    assert _resolve_deliverable(
        _spec(description="Implement a function that sums a column.")) == "code"


def test_every_resolution_is_a_legal_kind():
    for desc in ("anything at all", "", "Write docs", "Implement a module", "pytest suite"):
        assert _resolve_deliverable(_spec(description=desc)) in _DELIVERABLE_KINDS


def test_absent_function_name_is_treated_as_docs():
    """The spec builder DEFAULTS function_name to "_doc_section", so the underscore rule
    fires on absence too. Deliberate: it matches _is_impl_leg, and it errs open -- an
    unnamed leg is simply not gated. Pinned so the behavior is not "fixed" by accident."""
    assert _resolve_deliverable(_spec(function_name="_doc_section")) == "docs"


def test_real_function_name_lets_the_model_choose():
    """Structure is silent here (not test_, not _), so the grammar-constrained claim wins."""
    assert _resolve_deliverable(
        _spec(function_name="parse_csv", deliverable="code")) == "code"
    assert _resolve_deliverable(
        _spec(function_name="render_readme", deliverable="docs")) == "docs"


# --- the field drives L5 applicability ------------------------------------------------

def _l5_applies(spec):
    """Mirrors the live predicate in _verify_subtask_levels."""
    d = (getattr(spec, "deliverable", None) or "").strip().lower()
    if d:
        return d in ("code", "tests")
    desc = spec.description or ""
    return bool(_CODE_TASK_RE.search(desc)) and not _DOCS_TASK_RE.search(desc)


def test_docs_leg_is_never_gated_however_it_is_worded():
    """The exact wording that produced the live PARTIALs."""
    s = _spec(deliverable="docs",
              description="Write user documentation for the CLI tool. The documentation "
                          "should explain how to invoke the script.")
    assert _CODE_TASK_RE.search(s.description), "precondition: prose alone WOULD mis-gate it"
    assert not _l5_applies(s)


@pytest.mark.parametrize("desc", [
    "Provide a pytest test suite covering the CLI implementation.",   # missed the old regex
    "Write a pytest test suite that imports the module and tests it.",  # matched it
])
def test_test_legs_are_gated_consistently_regardless_of_phrasing(desc):
    """This is the coin flip the change exists to remove: same job, same verdict."""
    assert _l5_applies(_spec(deliverable="tests", description=desc))


def test_code_leg_still_gated():
    assert _l5_applies(_spec(deliverable="code", description="Implement main()."))


def test_missing_field_falls_back_to_the_legacy_regex_path():
    """A graph decomposed by an older build has no field -- it must still verify."""
    legacy_code = _spec(deliverable=None,
                        description="Implement a function that parses a CSV module.")
    legacy_docs = _spec(deliverable=None,
                        description="Write user documentation describing the tool.")
    assert _l5_applies(legacy_code)
    assert not _l5_applies(legacy_docs)


# --- grammar --------------------------------------------------------------------------

def test_schema_requires_deliverable_and_constrains_it():
    schema = _decompose_schema(require_function_name=True)
    item = schema["properties"]["subtasks"]["items"]
    assert "deliverable" in item["required"], "optional would re-open the coin flip"
    assert item["properties"]["deliverable"]["enum"] == _DELIVERABLE_KINDS


def test_deliverable_precedes_the_keys_the_model_backtracks_over():
    """Property ORDER is load-bearing for llama.cpp grammars (see _decompose_schema)."""
    keys = list(_decompose_schema(require_function_name=True)["properties"]["subtasks"]["items"]["properties"])
    assert keys.index("deliverable") < keys.index("acceptance_criteria")
    assert keys.index("description") < keys.index("deliverable")
