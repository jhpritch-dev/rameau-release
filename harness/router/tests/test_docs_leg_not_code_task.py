"""A documentation leg must not be routed to the L5 code gate.

WHY. `_CODE_TASK_RE`'s own comment warns it is "biased toward FALSE NEGATIVES ... a false
positive now FAILS a legitimate doc leg." On 2026-07-23 that false positive started firing
in production. The regex is `(implement|write|build|create) .* (function|class|script|
module)` and the `.*` spans sentences, so a real s2 leg reading

    "Write user documentation for the CLI tool. The documentation should explain how to
     invoke the script, describe each option ..."

matched `write` ... `script`. Its prose then went to the L5 gate, where execsvc extracted
the plain-fenced usage block the documentation is SUPPOSED to contain

    ```
    Usage: my_cli [OPTIONS] FILE
    Options:
      --help  Show this message and exit.
    ```

and compiled it as Python -- the fence regex makes the `python` tag optional, and execsvc's
extractor is byte-identical by design (subtask_graph.py:585). Result:
`compile FAIL: invalid syntax` -> leg FAILED -> take PARTIAL. Measured live: 3 PARTIAL takes
in one 45-minute window, every one of them s2 on 1a-hearth, every one this.

The strings below are VERBATIM live leg descriptions pulled from the router pod, not
invented examples. The s1/s3 cases are the regression guard that matters: the fix must not
un-gate real code legs, which would re-open the vacuous-gate hole execsvc v2 was built to
close.
"""
from subtask_graph import _CODE_TASK_RE, _DOCS_TASK_RE


def _is_code_task(desc: str) -> bool:
    """Mirrors the live predicate in _verify_subtask_levels."""
    return bool(_CODE_TASK_RE.search(desc)) and not _DOCS_TASK_RE.search(desc)


# --- live s2 descriptions: documentation deliverables ---------------------------------

DOC_LEGS = [
    "Write user documentation for the CLI tool. The documentation should explain how to "
    "invoke the script, describe each command-line option including --help, and list the "
    "error messages emitted for missing or malformed input.",
    "Write clear user documentation that explains how to invoke the CLI, describes each "
    "command-line option (including --help), outlines the expected CSV format, and details "
    "possible error messages for missing or malformed files.",
    "Write clear user documentation for the CLI tool, covering usage syntax, available "
    "flags (including --help), expected CSV format, description of each printed statistic, "
    "and detailed error-handling behavior with example commands.",
]

# --- live s1/s3 descriptions: genuine code deliverables -------------------------------

CODE_LEGS = [
    "Implement the full command-line interface. Create a `main(argv: list[str]) -> int` "
    "function that uses argparse to accept a CSV path.",
    "Create a small, self-contained helper function validate_path(path: str) -> bool that "
    "returns True only if the path points at a readable file.",
    "Implement a Python command-line tool that accepts a CSV file path, validates the path "
    "using a helper, reads the CSV with the csv module, and prints a formatted table.",
]


def test_doc_legs_are_not_code_tasks():
    for desc in DOC_LEGS:
        assert not _is_code_task(desc), f"doc leg mis-detected as code: {desc[:70]}"


def test_code_legs_still_gated():
    for desc in CODE_LEGS:
        assert _is_code_task(desc), f"code leg lost its gate: {desc[:70]}"


def test_the_exact_regression_string():
    """The single description that produced the live PARTIALs."""
    desc = ("Write user documentation for the CLI tool. The documentation should explain "
            "how to invoke the script, describe each option, and show example commands.")
    assert _CODE_TASK_RE.search(desc), "precondition: the old predicate DID match this"
    assert not _is_code_task(desc), "the fix must de-gate it"


def test_docs_predicate_does_not_swallow_a_code_leg_that_merely_mentions_docstrings():
    """`describe` and `document` are doc words; a docstring instruction is not a doc leg.

    Kept as a KNOWN LIMIT rather than a passing assertion: this predicate is deliberately
    deliverable-shaped and errs toward skipping the gate, exactly as _CODE_TASK_RE's
    comment prescribes. If a code leg is ever phrased "implement the function and document
    it", it loses L5 -- an ungated code leg, which is the status quo ante, not a new hole.
    """
    desc = "Implement a parser function and document it with a docstring."
    assert _CODE_TASK_RE.search(desc)
    assert not _is_code_task(desc)  # skipped: accepted trade, see docstring
