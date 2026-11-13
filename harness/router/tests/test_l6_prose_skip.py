"""BUG-5: L6 must know what KIND of deliverable it is grading before it compiles.

WHY. _verify_level6 compiled unconditionally. Prose does not compile, so every prose run
was graded "L6 FAIL: SyntaxError" and _needs_review fired the two-pass CODE reviewer at it
(whose prompt literally says "Assembled code to review:"). Measured 2026-07-16 against the
live function: a plain paragraph -> passed=False, two-pass fires. 100% of prose runs.

compile() cannot tell "broken code" from "text that was never code". So the kind is decided
FIRST -- mirroring the L4 skip already in the leg verify chain ("skip L4 for code -- L5
pytest handles code verification"): a check that does not APPLY returns a neutral result.

The inverse matters just as much: this must NOT start skipping real code. _ASSEMBLER_SYSTEM
tells the assembler "Output only valid Python. No markdown fences." -- so a real code
assembly is BARE, FENCE-LESS Python. A fence-based discriminator would skip every genuine
code assembly, which is the same bug inverted. Hence the signature alternatives.
"""
from autopilot_graph import _CODE_SHAPE_RE, _needs_review, _verify_level6

PROSE = ("The retry policy uses exponential backoff. Each attempt waits twice as long as "
         "the previous one, capped at 30 seconds. After five attempts the request is moved "
         "to the dead-letter queue for manual inspection by an operator.")

# What the assembler is INSTRUCTED to emit for a code task: bare Python, no fences.
BARE_CODE_OK = "import math\n\n\ndef area(r):\n    return math.pi * r * r\n"
BARE_CODE_BROKEN = "import math\n\n\ndef area(r:\n    return math.pi * r * r\n"


# --- the bug: prose must not be graded as a compile failure ---------------------------

def test_prose_is_skipped_not_failed():
    l6 = _verify_level6(PROSE)
    assert l6["passed"] is True, "prose must not be graded a compile failure"
    assert "SKIP" in l6["summary"]


def test_prose_no_longer_triggers_the_code_reviewer():
    """The regression this fix exists to kill."""
    l6 = _verify_level6(PROSE)
    needs, reason = _needs_review(PROSE, l6)
    assert needs is False, f"two-pass CODE review fired on prose: {reason!r}"


def test_skip_is_recorded_as_SKIP_not_as_PASS():
    """l6 is written to the decisions log AS THE GRADE. An un-run check is not a pass."""
    l6 = _verify_level6(PROSE)
    assert "SKIP" in l6["summary"]
    assert "L6 PASS" not in l6["summary"]


# --- the inverse: real code must still be graded ---------------------------------------

def test_bare_fenceless_code_still_compiles_and_passes():
    """The assembler emits NO fences. This must not be mistaken for prose."""
    l6 = _verify_level6(BARE_CODE_OK)
    assert l6["passed"] is True
    assert "SKIP" not in l6["summary"], "a real code assembly must not be skipped"


def test_bare_fenceless_BROKEN_code_still_FAILS():
    """The whole point of L6. Skipping this would be the fix inverted."""
    l6 = _verify_level6(BARE_CODE_BROKEN)
    assert l6["passed"] is False
    assert "SKIP" not in l6["summary"]
    needs, reason = _needs_review(BARE_CODE_BROKEN, l6)
    assert needs is True and "L6 failed" in reason


def test_fenced_code_still_graded():
    l6 = _verify_level6("```python\ndef f( : return 1\n```")
    assert l6["passed"] is False


# --- the discriminator itself -----------------------------------------------------------

def test_discriminator_is_line_anchored_not_substring():
    """Bare substrings are what made _is_code_task fire on doc legs. Prose contains
    'import' and 'class' as ordinary English."""
    for prose in ("The import of this decision is hard to overstate.",
                  "This is a class of bug we have seen before.",
                  "We import nothing from that module conceptually.",
                  PROSE):
        assert not _CODE_SHAPE_RE.search(prose), f"false positive on prose: {prose[:40]!r}"


def test_discriminator_matches_real_python_shapes():
    for code in ("def f():\n    pass",
                 "class A:\n    pass",
                 "import os",
                 "from x import y",
                 "async def f():\n    pass",
                 "@decorator\ndef f():\n    pass",
                 "```python\nx = 1\n```",
                 "    def indented():\n        pass"):
        assert _CODE_SHAPE_RE.search(code), f"false negative on code: {code[:30]!r}"


def test_empty_and_none_still_handled():
    """Pre-existing guards must survive the new branch."""
    assert _verify_level6("")["passed"] is False
    assert _verify_level6("   ")["passed"] is False
    assert _verify_level6(None)["passed"] is False
