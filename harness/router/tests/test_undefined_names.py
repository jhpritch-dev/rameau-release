"""The import-only gate cannot see a NameError. This closes that.

THE FINDING (2026-07-26, after four consecutive banks blamed the wrong leg):

Every s1 implementation leg PASSED on its first attempt in all four banks, with
`compiles + runs clean; NO TESTS PRESENT` -- while shipping this:

    def main(argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        args = parser.parse_args(argv)
        if not validate_path(args.file):        # never defined ANYWHERE
            print(..., file=sys.stderr)         # `import sys` only inside __main__
        ...

The L5 sandbox IMPORTS the module. The IMPORT SAFETY contract in `_build_messages` requires
that nothing execute at module scope -- "define functions... do NOT call them there... put
every executable statement behind `if __name__`". That rule is CORRECT, and it also means a
function body referencing a name that does not exist imports perfectly and scores green.

s4, the test leg, is the only leg that CALLS main(). So s4 was the only leg that surfaced
s1's defects -- and it was scored as the failure, take after take, for four banks. Two
sessions of prompt engineering were aimed at the test leg for a bug the implementation leg
wrote. The test leg is the implementation's only real gate, and the system was punishing it
for doing that job.

This is the THIRD vacuous-pass branch of the same family:
    D11  code leg with no code          -> was "skipped", now FAILS
    D12  tests leg with no tests        -> was "not credited", now FAILS
    here code leg never EXECUTED        -> passed on syntax, now FAILS

DESIGN CONSTRAINT: this gate FAILS legs, so a false positive is worse than a miss. Bindings
are collected FLAT (scope-insensitive), star-imports bail out entirely, and only bare Name
loads are considered. It flags exactly one thing: a name that is read and bound nowhere and
is not a builtin -- a guaranteed NameError the moment that line runs.
"""
import pytest

from subtask_graph import _undefined_called_names as undefined


# Verbatim from graph dep_context, v259 bank. Do not "fix" it -- it is the evidence.
REAL_BROKEN_S1 = '''
import argparse
import csv
import math
from statistics import median, mean

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="Path to CSV file")
    args = parser.parse_args(argv)

    if not validate_path(args.file):
        print(f"Error: {args.file} is not a valid CSV file.", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
'''


def test_the_real_s1_defect_is_caught():
    """The exact code that passed its gate four banks running."""
    assert "validate_path" in undefined(REAL_BROKEN_S1)


def test_the_real_s1_defect_imports_cleanly():
    """Proves WHY the old gate missed it: the module body is fine. Only calling main() breaks.

    If this ever starts failing, the import-only gate would have caught the defect on its own
    and this whole check is redundant. It does not.
    """
    import re

    stripped = re.sub(r"\nif\s+__name__\s*==\s*[\"']__main__[\"']\s*:\s*\n(?:[ \t]+.*\n?)*",
                      "\n", REAL_BROKEN_S1)
    ns = {}
    exec(compile(stripped, "<s1>", "exec"), ns)          # imports clean -- the whole problem
    with pytest.raises(NameError):
        ns["main"](["x.csv"])


def test_undefined_call_is_caught():
    assert undefined("def f(p):\n    return validate(p)\n") == ["validate"]


def test_module_used_but_never_imported_is_caught():
    assert undefined("def f():\n    return sys.argv\n") == ["sys"]


# --- false-positive battery: every one of these is LEGITIMATE Python -------------------
# This gate fails legs. A false positive here fails a leg that did nothing wrong, which is
# strictly worse than missing a defect -- so this list is the more important half of the file.

@pytest.mark.parametrize("label,src", [
    ("late binding / call-before-def",
     "def outer():\n    def inner(): return helper()\n    return inner\ndef helper(): return 1\n"),
    ("comprehension + walrus",
     "import os\ndef f(rows):\n    out = [x*2 for x in rows if (y := x) > 0]\n"
     "    d = {k: v for k, v in os.environ.items()}\n    return out, d, y\n"),
    ("class / self / decorators",
     "import functools\nclass A:\n    attr = 1\n    @functools.cache\n"
     "    def m(self, n): return self.attr + n\n    @staticmethod\n    def s(): return A.attr\n"),
    ("with / except-as / for targets",
     "import csv, sys\ndef g(p):\n    try:\n        with open(p) as fh:\n"
     "            for row in csv.reader(fh):\n                pass\n"
     "    except OSError as e:\n        print(e, file=sys.stderr)\n        return 1\n    return 0\n"),
    ("aliased and dotted imports",
     "import os.path as osp\nfrom statistics import mean as _mean\n"
     "def h(v): return osp.join('a','b'), _mean(v)\n"),
    ("typing annotations",
     "from typing import Optional, List\ndef t(a: 'List[int]', b: Optional[int] = None) -> int:\n"
     "    total: int = sum(a)\n    return total + (b or 0)\n"),
    ("args / kwargs / posonly / kwonly",
     "def outer(*args, **kwargs):\n    def inner(a, /, b, *, c=1): return a+b+c\n"
     "    return inner(*args, **kwargs)\n"),
    ("global declaration",
     "COUNT = 0\ndef bump():\n    global COUNT\n    COUNT += 1\n    return COUNT\n"),
])
def test_no_false_positive_on_legitimate_code(label, src):
    assert undefined(src) == [], f"false positive on {label}"


def test_star_import_bails_out_entirely():
    """`from x import *` makes bindings unknowable -> report nothing rather than guess.

    Erring open here is deliberate: the alternative is failing every leg that uses a star
    import for names that are in fact perfectly defined.
    """
    assert undefined("from math import *\ndef k(x): return sqrt(x) + mystery(x)\n") == []


def test_syntax_error_is_not_this_gates_job():
    """The compile gate already fails these -- do not double-report or crash on them."""
    assert undefined("def broken(:\n") == []


# --- the case the original battery MISSED -----------------------------------------------

# v260 fired "undefined name: main" 23 times against s4 TEST legs. `main` is not undefined --
# it is defined in s1's implementation, which execute_subtask_node INLINES into the sandbox
# payload before POSTing. Judging the leg's own fence in isolation flags the dependency call
# as undefined and fails the leg for doing exactly its job.
#
# A false positive in the one gate written specifically to avoid them. The original battery
# covered standalone modules only -- and the dependent leg is the case this subsystem exists
# to serve. The gate must analyze what the SANDBOX analyses: dep + leg, merged.

import subtask_graph
from bifrost_message import SubtaskResult, compute_checksum

_DEP_CONTEXT = """### Output of subtask s1

```python
import argparse

def validate_path(p: str) -> bool:
    return p.endswith(".csv")

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    args = parser.parse_args(argv)
    return 0 if validate_path(args.file) else 1
```
"""

_TEST_LEG = """```python
import pytest

def test_main_ok():
    assert main(["a.csv"]) == 0

def test_helper():
    assert validate_path("a.csv") is True
```"""


class _DepSpec:
    def __init__(self, dep_context):
        self.deliverable = "tests"
        self.description = "Write a pytest suite for the CLI."
        self.subtask_id = "s4"
        self.graph_id = "g1"
        self.dep_context = dep_context


def _gate_with_dep(sandbox_l5, output, dep_context):
    res = SubtaskResult(
        type="subtask_result", subtask_id="s4", graph_id="g1", status="PASS",
        output=output, output_checksum=compute_checksum(output),
        verification_level_reached=0, tier_used="2", tokens_used=1,
        escalation_artifact={"sandbox_l5": sandbox_l5},
    )
    return subtask_graph._l5_from_sandbox(res, output, _DepSpec(dep_context), True)


def test_test_leg_calling_its_dependency_is_NOT_flagged():
    """THE REGRESSION. `main` and `validate_path` live in the inlined dep, not the leg."""
    r = _gate_with_dep(
        {"executed": True, "passed": True, "syntax_ok": True,
         "summary": "2 passed", "tests_collected": 2},
        _TEST_LEG, _DEP_CONTEXT,
    )
    assert r["passed"] is True, f"false positive on a dependent test leg: {r['summary']}"


def test_a_genuinely_undefined_name_is_still_caught_with_a_dep_present():
    """Widening scope to the dep must not blind the gate."""
    bad = "```python\ndef test_x():\n    assert compute_stats([1]) == 1\n```"
    r = _gate_with_dep(
        {"executed": True, "passed": True, "syntax_ok": True,
         "summary": "1 passed", "tests_collected": 1},
        bad, _DEP_CONTEXT,
    )
    assert r["passed"] is False
    assert "compute_stats" in r["summary"]


def test_leg_with_no_dep_context_is_unaffected():
    """The s1 case -- no dependency -- must still catch its own undefined names."""
    bad = "```python\ndef main(a):\n    return validate_path(a)\n```"
    r = _gate_with_dep(
        {"executed": True, "passed": True, "syntax_ok": True, "summary": "ok",
         "tests_collected": 1},
        bad, None,
    )
    assert r["passed"] is False and "validate_path" in r["summary"]
