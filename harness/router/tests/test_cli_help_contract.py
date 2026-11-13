"""#159 -- the CLI leg must not be steered into an argparse conflict.

MEASURED, v355 csv bank: 6 of 15 CLI-leg verdicts (40%) cite argparse; s1 cites it 0 of 8
times. Nine of those citations land on argparse.py:1633, `_handle_conflict_error`.

The operator prompt asks for "a --help flag"; the decomposer copies that into the CLI leg's
description and acceptance criteria; a model that complies literally writes
`add_argument("--help", ...)`; ArgumentParser already owns -h/--help, so it raises. Because
it raises inside main(), every test calling main() errors at once.

test_argparse_still_conflicts_on_double_help is the load-bearing one: it pins the PREMISE
against the live interpreter. If a future Python stops raising here, that test fails and the
whole clause should be reconsidered rather than left as folklore -- the #149 lesson, where a
guard encoded a claim about an external component that had already changed.
"""
import argparse

import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import _build_messages

CLI_DESC = ("Implement main(argv) CLI entry point that calls csv_summary_stats and prints "
            "results in a readable format; supports --help flag showing usage; handles file "
            "not found and invalid CSV errors gracefully.")
PURE_DESC = ("Implement word_frequencies(text) that returns how many times each word appears, "
             "case-insensitive, split on whitespace. Pure function, stdlib only.")
# The REAL control: s1 from the same graph. File/CSV-shaped, so it takes the same #145
# file-task advice as the CLI leg, but it never asks for a help flag -- which is exactly why
# it cited argparse 0 of 8 times while the CLI leg cited it 6 of 15. Comparing against
# PURE_DESC instead would measure the file-task block too and say nothing about #159.
S1_DESC = ("Implement csv_summary_stats(input_file) function that reads a CSV file and "
           "returns summary stats (count, mean, std, min, max) for numeric columns; handle "
           "missing/invalid data gracefully with defaults; include tests in same module.")


def _spec(desc, fn="main", sig="argv: list[str]", out="int"):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g-159", subtask_id="s2", description=desc,
        acceptance_criteria=["parses CLI args correctly", "supports --help"],
        assigned_tier="1b", deliverable="code_selftest", dependencies=[],
        function_name=fn, input_signature=sig, output_signature=out,
    )


def _system(spec):
    msgs = _build_messages(spec, [])
    return "\n".join(m.get("content", "") for m in msgs if m.get("role") == "system")


def test_argparse_still_conflicts_on_double_help():
    """PIN THE PREMISE. The clause exists only because this raises."""
    p = argparse.ArgumentParser(prog="t")
    p.add_argument("input_file")
    with pytest.raises(argparse.ArgumentError) as e:
        p.add_argument("--help", action="store_true")
    assert "--help" in str(e.value)
    assert "conflict" in str(e.value).lower()


def test_a_help_flag_is_free_so_the_advice_is_satisfiable():
    """The fix we tell the model to use must actually work: add nothing, get --help."""
    p = argparse.ArgumentParser(prog="t", description="d")
    p.add_argument("input_file", help="the csv")
    assert "--help" in p.format_help()
    with pytest.raises(SystemExit) as e:      # argparse exits 0 after printing help
        p.parse_args(["--help"])
    assert e.value.code == 0


def test_cli_leg_is_warned_about_the_builtin_help():
    s = _system(_spec(CLI_DESC))
    assert "ARGPARSE --help" in s
    assert "ALREADY provides" in s
    assert "add NOTHING" in s


def test_a_leg_that_never_mentions_help_is_not_warned():
    """Narrow by design: envelope bytes are charged per leg, so a pure function pays none."""
    s = _system(_spec(PURE_DESC, fn="word_frequencies", sig="text: str", out="dict"))
    assert "ARGPARSE --help" not in s


def test_the_clause_names_the_error_the_model_will_see():
    """A caution the model cannot connect to its own traceback is wasted bytes."""
    s = _system(_spec(CLI_DESC))
    assert "ArgumentError" in s
    assert "conflicting option string" in s


def test_the_sibling_implementation_leg_is_not_warned():
    """s1 is file/CSV-shaped and still must not pay for this -- it never asks for --help."""
    s = _system(_spec(S1_DESC, fn="csv_summary_stats", sig="input_file: str", out="dict"))
    assert "ARGPARSE --help" not in s


def test_the_clause_is_small():
    """It rides in the same envelope the #152 margin alarm guards.

    Measured against S1_DESC, not a pure function: both descriptions take the #145 file-task
    block, so the difference isolates THIS clause instead of re-measuring that one.
    """
    with_help = len(_system(_spec(CLI_DESC)))
    without = len(_system(_spec(S1_DESC, fn="csv_summary_stats",
                                sig="input_file: str", out="dict")))
    grew = with_help - without
    assert 0 < grew < 400, "the argparse clause grew the system prompt by %d chars" % grew
