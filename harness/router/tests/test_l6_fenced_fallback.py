"""L6 fenced-block fallback: it must not grade a fragment as the whole artifact.

Regression cover for the 2026-08-07 finding that _verify_level6 returned "L6 PASS" on an
artifact whose unfenced half was broken Python, having compiled only the fenced 26 of 104
characters. The tests that MATTER here are the two guarding directions at once: the masking
case must now FAIL, and the legitimate markdown-wrapper case must still PASS -- a fix that
only satisfied the first could be had by deleting the fallback entirely.
"""
import autopilot_graph as A

B = chr(96) * 3


def _fence(code):
    return B + "python\n" + code + B


def test_prose_wrapping_fenced_code_still_passes():
    """The legitimate reason the fallback exists. Must not regress."""
    art = "Here is the module.\n\n" + _fence("def add(a, b):\n    return a + b\n") + "\nThat is all.\n"
    r = A._verify_level6(art)
    assert r["passed"] is True
    assert r["summary"] == "L6 PASS"
    # the discard must be visible in the evidence, not silent
    assert "discarded" in r["evidence"]


def test_broken_code_outside_the_fence_now_fails():
    """The masking case: unfenced broken Python + a valid fenced block."""
    art = ("def broken(:\n    x = 1\n    return x\n\n"
           "class AlsoBroken\n    pass\n\n"
           + _fence("def fine():\n    return 42\n"))
    r = A._verify_level6(art)
    assert r["passed"] is False
    assert "unverified code" in r["summary"]
    # and it must say how much it declined to vouch for
    assert "never compiled" in r["evidence"]


def test_no_fence_broken_code_fails_once():
    r = A._verify_level6("def broken(:\n    return 1\n")
    assert r["passed"] is False
    assert r["summary"].startswith("L6 FAIL: SyntaxError")


def test_fenced_code_that_is_itself_broken_fails():
    art = "Notes.\n\n" + _fence("def broken(:\n    return 1\n")
    r = A._verify_level6(art)
    assert r["passed"] is False
    assert r["summary"].startswith("L6 FAIL: SyntaxError")


def test_whole_file_valid_code_is_unaffected():
    """Early return above the fallback -- must not be perturbed."""
    r = A._verify_level6("def h():\n    return 1\n")
    assert r["passed"] is True
    assert r["summary"] == "L6 PASS"


def test_pure_prose_still_skips_neutral():
    """BUG-5 regression guard: prose must not be compiled at all."""
    r = A._verify_level6("This document explains the rollout plan in three parts.\n")
    assert r["passed"] is True
    assert "SKIP" in r["summary"]
