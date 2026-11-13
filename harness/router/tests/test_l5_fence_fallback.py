"""L5 must judge CODE, not markdown punctuation.

Regression battery for the v326-bank defect: `_l5_from_sandbox` required a markdown fence and
failed a leg whose code execsvc had just executed with its test passing.

Ground truth, probed against live execsvc 2026-08-10 (/execute, from inside the router pod):
    unfenced raw python -> passed=True  executed=True syntax_ok=True tests=1/1/0
    fenced ```python    -> passed=True  executed=True syntax_ok=True tests=1/1/0
    prose, no code      -> passed=False "compile FAIL: invalid syntax"
So: fence OPTIONAL, prose REJECTED. These tests encode exactly that contract.
"""
import sys, types, pytest
from pathlib import Path

# The tree this test lives in. A hardcoded repo path here put the real repo first on sys.path for
# every module imported after collection, so a suite run from any other copy tested the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtask_graph import _py_source_from_output, _l5_from_sandbox  # noqa: E402


CODE = (
    "import re\n"
    "from collections import Counter\n"
    "\n"
    "def top_n_words(text, n):\n"
    "    words = re.findall(r'[a-z]+', text.lower())\n"
    "    return sorted(Counter(words).items(), key=lambda kv: (-kv[1], kv[0]))[:n]\n"
    "\n"
    "def test_top_n_words():\n"
    "    assert top_n_words('a b a', 1) == [('a', 2)]\n"
)


# ---------------------------------------------------------------- extractor contract
def test_fenced_is_extracted():
    src, how = _py_source_from_output("```python\n" + CODE + "```")
    assert how == "fenced"
    assert "def top_n_words" in src


def test_unfenced_is_extracted():
    """THE REGRESSION. Unfenced python is what execsvc runs, so it is what L5 must see."""
    src, how = _py_source_from_output(CODE)
    assert how == "unfenced"
    assert "def top_n_words" in src


def test_prose_is_not_code():
    src, how = _py_source_from_output(
        "This module would return the most frequent words in the text."
    )
    assert how not in ("fenced", "unfenced"), how   # the gate's own predicate
    assert how == "prose"
    assert src == ""


def test_bare_word_is_not_code():
    """`Done` is a syntactically valid expression statement. It is not a deliverable.
    Guards the ast.parse fallback from degenerating into 'anything that parses'."""
    for junk in ("Done", "ok", "None", "42"):
        assert _py_source_from_output(junk)[1] not in ("fenced", "unfenced"), junk


def test_empty_is_not_code():
    assert _py_source_from_output("")[1] == "empty"
    assert _py_source_from_output("   \n  ")[1] == "empty"


def test_prose_wrapping_a_fence_still_extracts():
    src, how = _py_source_from_output("Here you go:\n\n```python\n" + CODE + "```\n\nHope that helps!")
    assert how == "fenced"
    assert "Hope that helps" not in src


# ---------------------------------------------------------------- gate behavior
class _Spec:
    def __init__(self, deliverable="code_selftest", dep_context=None):
        self.deliverable = deliverable
        self.dep_context = dep_context
        self.subtask_id = "s2"
        self.graph_id = "t"


class _Result:
    def __init__(self, artifact):
        self.escalation_artifact = artifact
        self.output = ""


def _sandbox(passed, collected=1, summary="compiles + runs clean; 1 test(s) passed"):
    return {"sandbox_l5": {"passed": passed, "tests_collected": collected,
                           "summary": summary, "stdout": "", "stderr": ""}}


def test_unfenced_code_with_passing_sandbox_is_not_failed():
    """The exact v326 shape: execsvc ran it green, output had no fence. Must NOT fail."""
    out = _l5_from_sandbox(_Result(_sandbox(True)), CODE, _Spec(), True)
    assert out["passed"] is True, out["summary"]


def test_unfenced_code_with_failing_sandbox_still_fails():
    """The fallback must not become a rubber stamp -- a real sandbox failure still fails."""
    out = _l5_from_sandbox(
        _Result(_sandbox(False, summary="1 of 1 test(s) FAILED")), CODE, _Spec(), True)
    assert out["passed"] is False
    assert "FAILED" in out["summary"]


def test_prose_from_a_code_leg_still_fails():
    """The no-fence branch existed to catch a code leg emitting prose. Keep that."""
    out = _l5_from_sandbox(_Result(_sandbox(True)), "I would implement this as follows.",
                           _Spec(), True)
    assert out["passed"] is False
    # SHAPE not wording: this assertion has already been re-pinned twice (it was
    # "no fenced code", then "no python source"). The contract is that a failing code
    # leg reports itself as an L5 code-leg failure; the sentence is free to change.
    assert out["summary"].startswith("L5: code leg ")


def test_non_code_leg_still_skipped():
    out = _l5_from_sandbox(_Result({}), "Some documentation prose.", _Spec("docs"), False)
    assert out["ran"] is False and out["passed"] is True


def test_undefined_name_gate_now_sees_unfenced_source():
    """SECOND-ORDER regression: static gates extracted via the fence regex, so on unfenced
    output they analyzed an EMPTY string and silently no-opped. Unfenced legs were
    UNDER-checked as well as over-failed."""
    bad = "def run():\n    return validate_path('x')\n"
    out = _l5_from_sandbox(_Result(_sandbox(True)), bad, _Spec(), True)
    assert out["passed"] is False, "undefined-name gate did not fire on unfenced source"
    assert "undefined name" in out["summary"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--no-header", "-x"]))
