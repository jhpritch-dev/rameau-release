"""s0 contract case-BINDING (deterministic fix, not just catch) -- 2026-07-28."""
import subtask_graph as sg

# A contract whose golden example pins argparse's lowercase 'usage:' (the v261/v265 class).
DEP = (
    "### CONTRACT (authoritative API -- signatures + golden I/O examples)\n\n"
    "```python\n"
    "def main(argv: list[str]) -> int: ...\n"
    '# golden: main(["--help"]) -> stdout contains "usage: prog [-h] file"; raises SystemExit(0)\n'
    "```\n\n---\n\n### Output of subtask s1\n\n```python\ndef main(argv): return 0\n```"
)

TEST_WRONG_CASE = (
    "```python\n"
    "import pytest, io, contextlib\n"
    "def test_help():\n"
    "    buf = io.StringIO()\n"
    "    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):\n"
    '        main(["--help"])\n'
    '    assert "Usage:" in buf.getvalue()\n'
    "```"
)


def test_mismatches_finds_the_case_typo():
    # _contract_case_mismatches takes RAW code (the bind helper extracts fences first).
    raw = TEST_WRONG_CASE.strip().removeprefix("```python\n").removesuffix("\n```")
    ms = sg._contract_case_mismatches(DEP, raw)
    assert ("Usage:", "usage:") in ms


def test_bind_rewrites_wrong_case_to_contract_spelling():
    corrected, applied = sg._bind_asserted_literals_to_contract(TEST_WRONG_CASE, DEP)
    assert ("Usage:", "usage:") in applied
    assert '"usage:"' in corrected and '"Usage:"' not in corrected
    # the rest of the test is untouched
    assert "redirect_stdout" in corrected and "SystemExit" in corrected


def test_bind_is_noop_when_assertion_matches_contract():
    good = TEST_WRONG_CASE.replace('"Usage:"', '"usage:"')
    corrected, applied = sg._bind_asserted_literals_to_contract(good, DEP)
    assert applied == []
    assert corrected == good


def test_bind_is_noop_without_contract():
    corrected, applied = sg._bind_asserted_literals_to_contract(TEST_WRONG_CASE, "no contract here")
    assert applied == [] and corrected == TEST_WRONG_CASE


def test_bind_handles_single_quotes():
    src = "```python\ndef test_x():\n    assert 'Usage:' in out\n```"
    corrected, applied = sg._bind_asserted_literals_to_contract(src, DEP)
    assert ("Usage:", "usage:") in applied
    assert "'usage:'" in corrected


def test_bind_leaves_genuinely_wrong_assertion_alone():
    # A literal that does NOT case-fold onto the contract must fall through to the sandbox.
    src = "```python\ndef test_x():\n    assert \"Frobnicate:\" in out\n```"
    corrected, applied = sg._bind_asserted_literals_to_contract(src, DEP)
    assert applied == [] and corrected == src
