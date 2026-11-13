"""The implementation leg must RUN its own entry point, not merely import it.

WHY THIS FILE EXISTS. L5 for a code leg IMPORTS the module: top-level executes, nothing is
CALLED. So an implementation that raises on its FIRST CALL scores "compiles + runs clean".
Measured, v267 bank -- s1 shipped

    parser.add_argument('--help', action='store_true')

on a parser that already auto-adds -h/--help, so every call raised

    ArgumentError: argument --help: conflicting option string: --help

s1 PASSED its own gate on all six takes while shipping that, and s4 -- the only leg that ever
executes s1's code -- failed and took the blame. The undefined-name gate catches missing names
statically; nothing caught runtime-on-first-call.

The harness rides the EXISTING runner: execsvc collects bare zero-argument `test_*` functions
and joins all fenced blocks, so a generated smoke test lands in the same module.
"""
import subtask_graph as sg
from bifrost_message import SubtaskSpec

_CONFLICT = '''import argparse

def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument('--help', action='store_true')   # conflicts with argparse's own -h/--help
    a = p.parse_args(argv)
    return 0
'''

_GOOD = '''import argparse

def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument('file')
    a = p.parse_args(argv)
    return 0
'''


def _spec(deliverable="code", fn="main"):
    return SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s1", description="build a CLI",
        acceptance_criteria=[], assigned_tier="1b", dependencies=[],
        function_name=fn, input_signature="argv: list[str]", output_signature="int",
        deliverable=deliverable,
    )


def _fenced(code):
    return "```python\n" + code + "```"


# --- the harness is generated for the right legs, and only those --------------------

def test_harness_generated_for_an_argparse_code_leg():
    got = sg._smoke_call_harness(_spec(), _fenced(_GOOD))
    assert got is not None
    entry, src = got
    assert entry == "main"
    assert "main(['--help'])" in src
    assert src.lstrip().startswith("def test_")      # must be COLLECTED by the runner
    assert "except SystemExit" in src                # --help exiting 0 is correct, not a failure


def test_no_harness_for_a_tests_leg():
    """s4 already calls the code; smoke-calling there would double-run and confuse blame."""
    assert sg._smoke_call_harness(_spec(deliverable="tests"), _fenced(_GOOD)) is None


def test_no_harness_when_the_leg_is_not_a_cli():
    """`--help` is meaningless to a non-argparse function -- calling it would fail the leg
    for the HARNESS's invented argument, not for the leg's own defect."""
    assert sg._smoke_call_harness(_spec(), _fenced("def main(argv):\n    return 0\n")) is None


def test_no_harness_when_arity_is_not_argv_shaped():
    two = "import argparse\ndef main(a, b):\n    return 0\n"
    assert sg._smoke_call_harness(_spec(), _fenced(two)) is None
    zero = "import argparse\ndef main():\n    return 0\n"
    assert sg._smoke_call_harness(_spec(), _fenced(zero)) is None


def test_no_harness_on_unparseable_code():
    assert sg._smoke_call_harness(_spec(), _fenced("def broken(:\n")) is None


# --- entry selection ----------------------------------------------------------------

def test_entry_prefers_the_specs_function_name():
    code = "import argparse\ndef run(argv): ...\ndef main(argv): ...\n"
    assert sg._smoke_entry(code, "run") == "run"


def test_entry_falls_back_to_main_then_to_a_sole_candidate():
    code = "import argparse\ndef helper(argv): ...\ndef main(argv): ...\n"
    assert sg._smoke_entry(code, "nonexistent") == "main"
    assert sg._smoke_entry("def only(argv): ...", None) == "only"


def test_entry_is_none_when_ambiguous():
    """Two equally plausible one-arg functions and no hint -- guessing would fail legs for
    the harness's choice. Returning None means no smoke test, which is the safe direction."""
    code = "import argparse\ndef alpha(argv): ...\ndef beta(argv): ...\n"
    assert sg._smoke_entry(code, None) is None


def test_private_functions_are_never_the_entry():
    assert sg._smoke_entry("def _hidden(argv): ...", None) is None


# --- the harness ACTUALLY CATCHES the measured defect (the load-bearing test) --------

def _run_module_then_smoke(code, spec=None):
    """Mirror execsvc: exec the module, then call every zero-arg test_* it defines."""
    got = sg._smoke_call_harness(spec or _spec(), _fenced(code))
    assert got is not None, "no harness generated"
    ns = {}
    exec(compile(code + "\n\n" + got[1], "<merged>", "exec"), ns)   # import-equivalent
    failures = []
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except BaseException as e:                 # what the runner records
                failures.append(f"{name}: {type(e).__name__}: {e}")
    return failures


def test_smoke_catches_the_v267_argparse_conflict():
    failures = _run_module_then_smoke(_CONFLICT)
    assert failures, "the --help conflict must FAIL the implementation leg"
    assert "ArgumentError" in failures[0], failures


def test_smoke_passes_a_healthy_cli():
    """--help raises SystemExit(0); that is correct and must NOT fail the leg."""
    assert _run_module_then_smoke(_GOOD) == []


def test_import_alone_does_NOT_catch_it():
    """The regression this gate exists for: importing the broken module is clean, which is
    exactly why L5 scored it 'compiles + runs clean' six takes running."""
    ns = {}
    exec(compile(_CONFLICT, "<import-only>", "exec"), ns)   # no exception
    assert "main" in ns
