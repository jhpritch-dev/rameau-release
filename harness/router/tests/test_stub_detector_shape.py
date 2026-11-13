"""Stub detection matches the SHAPE of a stub, not the presence of a word.

WHY. _STUB_PATTERNS was a bare substring list including "TODO" and "FIXME", scanned over
the assembled deliverable with no doc-vs-code branch. Two consequences:

  - any PROSE containing "TODO" fired the two-pass CODE reviewer (_REVIEW_SYSTEM's prompt
    is literally "Assembled code to review:");
  - inside code, any docstring merely MENTIONING TODO matched -- a TODO-list manager's own
    module docstring would have triggered a review of itself on every run.

Third instance of one habit, now a category: a content word used as a task-type signal
where the discriminator should be structural. See _is_code_task (subtask_graph, Change 4)
and _verify_level6 (BUG-5).
"""
from autopilot_graph import _needs_review, _verify_level6

L6_PASS = {"passed": True, "summary": "L6 PASS"}

PROSE_WITH_TODO = (
    "The migration plan has three phases. Phase two is still TODO pending the operator's "
    "decision on capacity, and the FIXME list from the last review is attached below."
)
CODE_CLEAN = "import math\n\n\ndef area(r):\n    return math.pi * r * r\n"


# --- the residual this fixes ------------------------------------------------------------

def test_prose_containing_TODO_does_not_trigger_the_code_reviewer():
    needed, reason = _needs_review(PROSE_WITH_TODO, L6_PASS)
    assert needed is False, f"CODE reviewer fired on prose mentioning TODO: {reason!r}"


def test_prose_end_to_end_through_the_real_L6():
    """Belt and braces: the real L6 skips prose, and the stub scan must not undo that."""
    l6 = _verify_level6(PROSE_WITH_TODO)
    needed, reason = _needs_review(PROSE_WITH_TODO, l6)
    assert needed is False, f"prose triggered review: {reason!r}"


def test_code_docstring_mentioning_TODO_does_not_trigger():
    """A TODO-list app must not review itself forever."""
    code = ('"""Manage the user\'s TODO list and FIXME backlog."""\n\n\n'
            "TODO_ITEMS = []\n\n\ndef add_todo(item):\n    TODO_ITEMS.append(item)\n")
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is False, f"docstring mention triggered review: {reason!r}"


# --- real stubs must STILL be caught -----------------------------------------------------

def test_real_todo_comment_still_triggers():
    code = "def f():\n    # TODO: implement this properly\n    return 1\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True and "TODO" in reason


def test_real_fixme_comment_still_triggers():
    code = "def f():\n    x = 1  # FIXME broken rounding\n    return x\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True and "FIXME" in reason


def test_not_implemented_still_triggers():
    code = "def f():\n    raise NotImplementedError\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True and "NotImplementedError" in reason


def test_pass_stub_still_triggers():
    code = "def f():\n    pass  # placeholder\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True


def test_ellipsis_stub_still_triggers():
    code = "def f():\n    ...  # to be filled in\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True


def test_return_none_stub_still_triggers():
    code = "def f():\n    return None  # placeholder\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True


def test_assembler_fallback_note_still_triggers():
    code = "import os\n\n# Note: Subtask s2 failed; best-effort replacement below\n\n\ndef f():\n    return 1\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True and "Note: Subtask" in reason


def test_leftover_contract_header_still_triggers():
    code = "# contract: returns int\n\n\ndef f():\n    return 1\n"
    needed, reason = _needs_review(code, L6_PASS)
    assert needed is True and "contract:" in reason


# --- clean code must not trigger ---------------------------------------------------------

def test_clean_code_does_not_trigger():
    needed, reason = _needs_review(CODE_CLEAN, L6_PASS)
    assert needed is False, reason


def test_word_TODOS_is_not_a_TODO_marker():
    code = "def f():\n    # TODOS were migrated to the tracker\n    return 1\n"
    needed, _ = _needs_review(code, L6_PASS)
    assert needed is False


# --- L6 failure still wins ---------------------------------------------------------------

def test_l6_failure_still_reported_before_stub_scan():
    needed, reason = _needs_review(CODE_CLEAN, {"passed": False, "summary": "SyntaxError"})
    assert needed is True and reason.startswith("L6 failed:")
