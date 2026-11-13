"""s0 contract-leg prototype -- wiring + gate unit tests (2026-07-27)."""
import autopilot_graph as ag
import subtask_graph as sg
from bifrost_message import SubtaskSpec


def _spec(sid, fn, deliverable, deps=None, desc="do a thing"):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id=sid, description=desc,
        acceptance_criteria=[], assigned_tier="1b", dependencies=deps or [],
        function_name=fn, input_signature="argv: list[str]", output_signature="int",
        deliverable=deliverable,
    )


def test_contract_symbols_public_toplevel_only():
    code = "def foo(x): ...\nclass Bar: ...\ndef _priv(): ...\n\ndef _hidden(): ...\n"
    assert sg._contract_symbols(code) == {"foo", "Bar"}
    assert sg._contract_symbols("just prose, no code") == set()


def test_contract_symbols_from_dep_context_slices_the_section():
    dep = ("### Output of subtask s1\n\n```python\ndef other(): ...\n```\n\n---\n\n"
           "### CONTRACT (authoritative API)\n\n```python\ndef main(argv): ...\nclass Cfg: ...\n```")
    assert sg._contract_symbols_from_dep_context(dep) == {"main", "Cfg"}
    assert sg._contract_symbols_from_dep_context("no contract here") == set()


def test_inject_contract_leg_wires_impl_and_test():
    subtasks = {
        "s1": _spec("s1", "summarize", "code"),
        "s2": _spec("s2", "_doc_section", "docs"),
        "s4": _spec("s4", "test_summarize", "tests", deps=["s1"]),
    }
    ag._inject_contract_leg(subtasks)
    assert "s0" in subtasks, "contract leg not injected"
    s0 = subtasks["s0"]
    assert s0.deliverable == "contract"
    assert s0.dependencies == []                       # runs first
    assert ag._resolve_deliverable(s0) == "contract"   # stamping preserves it
    # impl now depends on and binds to the contract
    assert subtasks["s1"].contract_dep == "s0"
    assert "s0" in subtasks["s1"].dependencies
    # test binds to the contract AND keeps the impl dep (so real code inlines at verify)
    assert subtasks["s4"].contract_dep == "s0"
    assert set(subtasks["s4"].dependencies) == {"s0", "s1"}


def test_inject_is_noop_without_impl_test_pair():
    subtasks = {"s1": _spec("s1", "summarize", "code"),
                "s2": _spec("s2", "_doc_section", "docs")}
    ag._inject_contract_leg(subtasks)
    assert "s0" not in subtasks
    assert not any((s.deliverable or "") == "contract" for s in subtasks.values())


def test_conformance_gate_fails_impl_missing_contract_symbol():
    # impl leg whose dep_context carries a CONTRACT pinning main() + Cfg, but the leg defines
    # only main -> FAIL naming the missing symbol.
    dep = "### CONTRACT (authoritative API)\n\n```python\ndef main(argv): ...\nclass Cfg: ...\n```"
    spec = _spec("s1", "summarize", "code", deps=["s0"])
    spec = spec.model_copy(update={"dep_context": dep, "contract_dep": "s0"})
    out = "```python\ndef main(argv):\n    return 0\n```"

    class _R:  # minimal result stand-in; conformance gate returns before reading it
        escalation_artifact = {}
    r = sg._l5_from_sandbox(_R(), out, spec, True)
    assert r["passed"] is False
    assert "Cfg" in r["summary"] and "does not define contract symbol" in r["summary"]


def test_conformance_gate_passes_when_impl_defines_all():
    dep = "### CONTRACT (authoritative API)\n\n```python\ndef main(argv): ...\n```"
    spec = _spec("s1", "summarize", "code", deps=["s0"])
    spec = spec.model_copy(update={"dep_context": dep, "contract_dep": "s0"})
    out = "```python\ndef main(argv):\n    return 0\n```"

    class _R:
        escalation_artifact = {}   # no sandbox verdict -> falls through to local fallback (ran may vary)
    r = sg._l5_from_sandbox(_R(), out, spec, True)
    # The conformance gate must NOT be the thing that fails it.
    assert "does not define contract symbol" not in r.get("summary", "")


def test_contract_shape_gate_rejects_prose_contract():
    spec = _spec("s0", "_contract", "contract")
    r = sg._l5_from_sandbox(object(), "Here is a description of the API, in prose.", spec, False)
    assert r["passed"] is False and "pinned no public API" in r["summary"]


def test_contract_shape_gate_accepts_real_stub():
    spec = _spec("s0", "_contract", "contract")
    r = sg._l5_from_sandbox(object(), "```python\ndef main(argv): ...\n```", spec, False)
    assert r["passed"] is True and "contract pins 1" in r["summary"]


def test_clean_contract_output_strips_double_fence_and_trailing_prose():
    # The v263 root cause: s0 double-closed its fence (```\n```) and appended junk.
    raw = "```python\ndef main(argv): ...\n```\n```\n\nsome trailing prose"
    assert ag._clean_contract_output(raw) == "def main(argv): ..."
    assert ag._clean_contract_output("no fence, just prose") == "no fence, just prose"


def test_clean_contract_keeps_dep_extraction_aligned():
    # After cleaning, a contract section + s1 section must BOTH extract. The v263 crash was that
    # s0's stray fence swallowed s1's opening fence, so s1's code was lost from the merge.
    import ast
    s0_raw = "```python\ndef main(argv): ...\n```\n```"          # double-closed, like v263
    contract_section = "### CONTRACT (x)\n\n```python\n" + ag._clean_contract_output(s0_raw) + "\n```"
    s1_section = "### Output of subtask s1\n\n```python\ndef main(argv):\n    return 0\n```"
    dep = contract_section + "\n\n---\n\n" + s1_section
    blocks = sg._parseable_py_blocks(dep)
    assert len(blocks) == 2, blocks
    for b in blocks:
        ast.parse(b)


# --- exit-code ambiguity gate (v267) -------------------------------------------------
#
# v266 bank: 7 of 10 s4 assertion failures were `Failed: DID NOT RAISE SystemExit`. The
# contract described behavior in PROCESS terms ("Exit code: 1"), the test read that as
# SystemExit(1), and the implementation returned instead. Measured with BOTH annotations --
# v265's contract said `-> int`, take 3's said `-> None` -- so the signature does not
# disambiguate it. Fail s0 (one cheap attempt) instead of s4 (four attempts + 600s deadline).

def _contract_spec():
    return _spec("s0", "_contract", "contract")


def test_exit_code_phrasing_is_rejected():
    out = ("```python\ndef main(argv: list[str]) -> int: ...\n"
           "# python csv_stats.py missing.csv\n# Error: File not found\n# Exit code: 1\n```")
    r = sg._l5_from_sandbox(object(), out, _contract_spec(), False)
    assert r["passed"] is False
    assert "exit code" in r["summary"].lower()
    assert "returns 1" in r["summary"] or "returns" in r["summary"]


def test_exit_code_gate_is_case_and_space_insensitive():
    for phrasing in ("Exit code: 0", "exit code 2", "EXIT  CODE: 1", "ExitCode: 1"):
        out = f"```python\ndef main(argv) -> int: ...\n# {phrasing}\n```"
        r = sg._l5_from_sandbox(object(), out, _contract_spec(), False)
        assert r["passed"] is False, phrasing


def test_returns_or_raises_phrasing_is_accepted():
    out = ("```python\ndef main(argv: list[str]) -> int: ...\n"
           "# main(['data.csv'])    returns 0; stdout 'Rows: 5'\n"
           "# main(['missing.csv']) returns 1; stderr 'Error: File not found'\n"
           "# main(['--help'])      raises SystemExit(0); stdout 'usage: '\n```")
    r = sg._l5_from_sandbox(object(), out, _contract_spec(), False)
    assert r["passed"] is True and "contract pins 1" in r["summary"]


def test_prose_contract_still_reports_the_prose_failure_not_the_exit_code_one():
    """Ordering guard: a contract that is prose AND says 'exit code' must fail for pinning no
    API -- the more fundamental defect -- so the operator is not sent chasing wording."""
    r = sg._l5_from_sandbox(object(), "It returns an Exit code: 1 on error.", _contract_spec(), False)
    assert r["passed"] is False and "pinned no public API" in r["summary"]


# --- contract case-binding gate (v266) -----------------------------------------------
#
# v265 bank, graph b5b570ea: s0 correctly pinned argparse's lowercase `usage:` and s4 asserted
# `"Usage:"`. One capital letter was the ONLY failing assertion, and it survived 4 attempts
# across 2 tiers despite each retry carrying a traceback naming the line.

_CONTRACT_DEP = (
    "### CONTRACT (authoritative API)\n\n"
    "```python\ndef main(argv): ...\n# usage: csv_stats.py [-h] file\n# Error: File not found\n```"
    "\n\n---\n\n"
    "### Output of subtask s1\n\n```python\ndef main(argv):\n    return 0\n```"
)


def _test_leg_code(assertion):
    """RAW python -- what _contract_case_mismatch receives (the caller extracts fences first)."""
    return ("import io\nimport contextlib\n\n"
            "def test_help():\n"
            "    buf = io.StringIO()\n"
            "    with contextlib.redirect_stdout(buf):\n"
            "        main(['--help'])\n"
            f"    assert {assertion} in buf.getvalue()\n")


def _test_leg_output(assertion):
    """The same code as a leg's fenced OUTPUT -- what _l5_from_sandbox receives."""
    return "```python\n" + _test_leg_code(assertion) + "```"


def test_case_mismatch_catches_the_measured_usage_bug():
    hit = sg._contract_case_mismatch(_CONTRACT_DEP, _test_leg_code('"Usage:"'))
    assert hit == ("Usage:", "usage:")


def test_case_mismatch_silent_when_the_assertion_matches_exactly():
    assert sg._contract_case_mismatch(_CONTRACT_DEP, _test_leg_code('"usage:"')) is None


def test_case_mismatch_ignores_a_literal_absent_from_the_contract():
    """A genuinely wrong assertion does not case-fold onto the contract -- it must fall
    through to the sandbox verdict, not be mislabelled a casing slip."""
    assert sg._contract_case_mismatch(_CONTRACT_DEP, _test_leg_code('"Rows: 42"')) is None


def test_case_mismatch_skips_short_literals_as_noise():
    """Below the 4-char noise floor a case-fold match is more likely coincidence than intent.
    Note `"[-H]"` is NOT noise -- it is 4 chars and binds to the contract's `[-h]`, so the
    gate catching it is a true positive, covered below."""
    assert sg._contract_case_mismatch(_CONTRACT_DEP, _test_leg_code('"-H"')) is None


def test_case_mismatch_catches_a_flag_spelled_in_the_wrong_case():
    assert sg._contract_case_mismatch(_CONTRACT_DEP, _test_leg_code('"[-H]"')) == ("[-H]", "[-h]")


def test_case_mismatch_needs_a_contract():
    assert sg._contract_case_mismatch(None, _test_leg_code('"Usage:"')) is None
    assert sg._contract_case_mismatch("### Output of subtask s1\n\n```python\nx = 1\n```",
                                      _test_leg_code('"Usage:"')) is None


def test_asserted_str_literals_only_reads_assert_statements():
    code = 'x = "not an assert"\ndef f():\n    assert "yes" in x, "msg"\n'
    assert set(sg._asserted_str_literals(code)) == {"yes", "msg"}
    assert sg._asserted_str_literals("def broken(:") == []


def test_gate_fires_through_l5_for_a_test_leg():
    spec = _spec("s4", "test_main", "tests", deps=["s0", "s1"])
    spec = spec.model_copy(update={"dep_context": _CONTRACT_DEP, "contract_dep": "s0"})

    class _R:
        escalation_artifact = {}
    r = sg._l5_from_sandbox(_R(), _test_leg_output('"Usage:"'), spec, True)
    assert r["passed"] is False
    # The message must name BOTH spellings -- the whole point is that a bare AssertionError
    # already failed to communicate this four times.
    assert "'Usage:'" in r["summary"] and "'usage:'" in r["summary"]


def test_gate_does_not_fire_for_a_conforming_test_leg():
    spec = _spec("s4", "test_main", "tests", deps=["s0", "s1"])
    spec = spec.model_copy(update={"dep_context": _CONTRACT_DEP, "contract_dep": "s0"})

    class _R:
        escalation_artifact = {}
    r = sg._l5_from_sandbox(_R(), _test_leg_output('"usage:"'), spec, True)
    assert "different case" not in r.get("summary", "")


def test_parseable_py_blocks_drops_nonpython_backstop():
    # Backstop: even if a markdown block leaks into the fence stream, it is dropped.
    dep = "```python\ndef ok(): ...\n```\n\n```\n### not code\n---\n```"
    assert sg._parseable_py_blocks(dep) == ["def ok(): ..."]
