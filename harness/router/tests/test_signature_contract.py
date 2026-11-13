"""Impl legs are pinned to their DECLARED signature so a separately-written test leg agrees.

WHY. The impl and test legs are produced by DIFFERENT models. Live proof (graph 9965f57c,
router v252): the decomposer declared `main(argv: list[str]) -> int`, the test leg called
`main([...])` accordingly, but the impl model wrote `def main():` and read sys.argv itself ->
`TypeError: main() takes 0 positional arguments but 1 was given` -> PARTIAL. Stating the exact
signature to the impl leg makes both sides derive from one contract.
"""
from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages


def _spec(deliverable, fn, insig="", outsig=""):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s1",
        description="Implement the tool.", acceptance_criteria=["x"],
        assigned_tier="2_qwen_coder", complexity_band="MODERATE", dependencies=[],
        context_budget_tokens=8192, deliverable=deliverable, function_name=fn,
        input_signature=insig, output_signature=outsig,
    )


def _system(spec):
    return _build_messages(spec, [], [])[0]["content"]


def test_impl_leg_gets_exact_signature():
    s = _system(_spec("code", "main", "argv: list[str]", "int"))
    assert "SIGNATURE CONTRACT" in s
    assert "def main(argv: list[str]) -> int:" in s


def test_impl_leg_no_return_type_still_pins_name_and_args():
    s = _system(_spec("code", "validate_path", "path: str", ""))
    assert "def validate_path(path: str):" in s


def test_docs_leg_is_not_pinned():
    # `_doc_section` is the doc/analysis convention -- it has no callable contract to honor.
    s = _system(_spec("docs", "_doc_section", "context: str", "str"))
    assert "SIGNATURE CONTRACT" not in s


def test_test_leg_is_not_pinned():
    s = _system(_spec("tests", "test_main", "", ""))
    assert "SIGNATURE CONTRACT" not in s
    assert "HOW YOUR TESTS ARE RUN" in s


def test_impl_contract_forbids_reading_sys_argv():
    """The exact live failure mode: the impl read sys.argv instead of taking the argument."""
    s = _system(_spec("code", "main", "argv: list[str]", "int"))
    assert "do not read `sys.argv`" in s
    assert "RETURN the declared value" in s


def test_impl_leg_gets_stdlib_only_contract():
    """Live proof (graph cd9a4e5e): impl imported pandas into a zero-dependency sandbox."""
    s = _system(_spec("code", "main", "argv: list[str]", "int"))
    assert "STDLIB ONLY" in s
    assert "pandas" in s


def test_docs_leg_has_no_stdlib_contract():
    s = _system(_spec("docs", "_doc_section", "context: str", "str"))
    assert "STDLIB ONLY" not in s
