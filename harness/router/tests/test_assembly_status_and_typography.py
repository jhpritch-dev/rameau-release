"""
Guards for the v220 repairs: status honesty (defect 3) and typography repair (defect 5).

DEFECT 3 -- assemble_node reported `"COMPLETE" if not failed else "COMPLETE"`, a ternary
that returns COMPLETE on both branches. A 1-of-3 run reported green while
`human_review_required` right beside it correctly said True. Four sites lied: RFP, PROSE,
main assemble, and the fallback (which returned COMPLETE while setting an `error` because
the assembler had thrown).

PARTIAL is safe on the wire: main.py's /result special-cases only QUEUED/RUNNING (202) and
FAILED (500), so PARTIAL returns 200 with the assembled output exactly like COMPLETE. The
Portal is the other half of this change (ArchitectFanout.jsx TERMINAL/HAS_RESULT,
BifrostPortal.jsx poll loop) -- shipping the router alone makes the Portal spin to its
wall-timeout and render TIMEOUT without ever fetching the output. Worse than the lie.

DEFECT 5 -- the diagnosis prescribed "ASCII normalization before compile()". Done
literally that is a FALSE GREEN: it normalizes the compile() input while still shipping
un-normalized output, so L6 reports PASS on code the user cannot run. _repair_code_typography
instead repairs the ARTIFACT, and only when typography is provably the sole defect.
"""
import logging

import pytest

from autopilot_graph import _assembly_status, _repair_code_typography


# --- defect 3: status honesty ------------------------------------------------------

def test_clean_run_is_complete():
    assert _assembly_status({}) == "COMPLETE"


def test_any_failure_is_partial():
    assert _assembly_status({"s2": "boom"}) == "PARTIAL"


def test_the_1_of_3_run_that_reported_green():
    """Graph 94957da1: 1 done / 2 failed -> reported COMPLETE."""
    assert _assembly_status({"s1": "x", "s2": "y"}) == "PARTIAL"


def test_degraded_is_partial_even_with_no_failed_subtasks():
    """The fallback path: the assembler THREW and a raw concatenation was substituted."""
    assert _assembly_status({}, degraded=True) == "PARTIAL"


@pytest.mark.parametrize("empty", [{}, None, [], ""])
def test_falsy_failed_containers_are_complete(empty):
    assert _assembly_status(empty) == "COMPLETE"


def test_status_is_never_the_no_op_ternary_again():
    import ast, inspect, pathlib
    src = pathlib.Path(inspect.getfile(_assembly_status)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.IfExp):
            seg = ast.get_source_segment(src, node) or ""
            body = ast.get_source_segment(src, node.body) or ""
            orelse = ast.get_source_segment(src, node.orelse) or ""
            if "COMPLETE" in seg and body.strip() == orelse.strip():
                pytest.fail(f"no-op ternary is back at line {node.lineno}: {seg}")


# --- defect 5: typography repair, safe by construction -----------------------------

def test_valid_code_is_never_touched():
    """A curly apostrophe inside a STRING LITERAL is legal and intentional content.

    Normalizing it would silently corrupt the user's data. Valid code compiles, so the
    helper returns before ever looking at typography.
    """
    src = 'x = "don\u2019t"\n'
    assert _repair_code_typography(src) == src


def test_smart_quote_as_delimiter_is_repaired():
    """The actual defect: U+2019 in a CODE position -> SyntaxError."""
    broken = "x = \u2019hello\u2019\n"
    fixed = _repair_code_typography(broken)
    assert fixed != broken
    compile(fixed, "<t>", "exec")  # raises if the repair failed


def test_prose_is_never_touched():
    """Prose never compiles either way, so the helper must leave documents alone."""
    src = "This is a summary \u2014 with an em dash.\n"
    assert _repair_code_typography(src) == src


def test_non_ascii_in_docstring_is_left_alone():
    """The real v219 output: U+2011 inside a docstring. Legal Python -- compiles fine.

    This is why "defect 5 confirmed live" was wrong: presence of non-ASCII is not breakage.
    """
    src = '"""Non\u2011numeric rows are ignored."""\nimport csv\n'
    assert _repair_code_typography(src) == src
    compile(src, "<t>", "exec")


def test_genuinely_broken_code_is_not_masked():
    """If normalizing does NOT make it compile, return it untouched so L6 still FAILS.

    Masking a real syntax error would be worse than the bug being fixed.
    """
    broken = "def f(:\n"
    assert _repair_code_typography(broken) == broken
    with pytest.raises(SyntaxError):
        compile(_repair_code_typography(broken), "<t>", "exec")


def test_typography_plus_real_error_is_not_masked():
    """Both defects present -> normalizing cannot save it -> must stay untouched."""
    broken = "x = \u2019hello\u2019\ndef f(:\n"
    assert _repair_code_typography(broken) == broken


@pytest.mark.parametrize("bad", ["", "   ", None, 42])
def test_non_string_and_empty_are_safe(bad):
    assert _repair_code_typography(bad) == bad


def test_syntaxerror_path_survives_debug_logging(caplog):
    """The SyntaxError branch must not raise when DEBUG logging is enabled.

    Regression: an overnight Session-O run added `log.debug(msg, text=fixed)` to this
    branch. `log` is a STDLIB logger (autopilot_graph.py:41), not structlog, so the
    kwarg raised `TypeError: Logger._log() got an unexpected keyword argument 'text'`
    -- but only when DEBUG was enabled, which no existing test turned on. The branch
    looked covered while the defect sat latent in the assembly path.
    """
    broken = "x = ’hello’\ndef f(:\n"
    with caplog.at_level(logging.DEBUG, logger="bifrost.autopilot"):
        assert _repair_code_typography(broken) == broken
