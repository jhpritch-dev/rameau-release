"""CC-lane item 3: the MoA confidence gate's truncation test was inverted.

Packet: coordination/pending/PACKET-a6-truncation-test-is-inverted-2026-09-04.md

Defect: strategies.py:162 defined

    UNCLOSED_BLOCK_RE = re.compile(r"```[\\w]*\\n(?!.*```)", re.DOTALL)

and strategies.py:197 escalated the request whenever `.search(draft_text)` hit.
The lookahead asks "is there no further ``` anywhere after this one" -- which is
not "is this block closed". A CLOSING fence satisfies that lookahead whenever it
is the last fence in the text, so a well-formed answer that opened a block,
closed it, and kept writing was reported as truncated and sent to stage 2.
Per the packet this was the single largest escalation reason in production
(5 of 13 rows in pipeline_stages), with measured precision 0 of 4.

These tests do NOT merely exercise the replacement. Every case is also run
against a locally reconstructed copy of the OLD pattern, so the suite shows the
old behavior FAILING the same inputs the new one passes. Without that arm a
green run would prove only that the new code agrees with itself.
"""

import pathlib
import re

import pytest

import strategies
from config import ComplexityBand

REPO = pathlib.Path(__file__).resolve().parent.parent
STRATEGIES = REPO / "strategies.py"

BT = "`" * 3          # a backtick fence, built rather than typed, so this file
TL = "~" * 3          # can be read in a markdown context without confusion
BT4 = "`" * 4

# The exact pattern that shipped from the router's first commit until 2026-09-09.
# Reconstructed here, not imported, because the name is gone from strategies.py.
OLD_PATTERN = re.compile(BT + r"[\w]*\n(?!.*" + BT + ")", re.DOTALL)


def old_says_truncated(text: str) -> bool:
    return bool(OLD_PATTERN.search(text))


# ---------------------------------------------------------------------------
# The corpus. `expect` is the RIGHT answer, fixed by reading each string, not by
# running either implementation.
# ---------------------------------------------------------------------------

CASES = [
    ("one_closed_block_prose_after",
     "Here you go:\n" + BT + "python\nprint(1)\n" + BT + "\nThat is the answer.\n",
     False),
    ("one_closed_block_at_end",
     "Here you go:\n" + BT + "python\nprint(1)\n" + BT + "\n",
     False),
    ("two_closed_blocks",
     "First:\n" + BT + "py\na=1\n" + BT + "\nThen:\n" + BT + "py\nb=2\n" + BT + "\nDone.\n",
     False),
    ("three_closed_blocks",
     "A\n" + BT + "py\na\n" + BT + "\nB\n" + BT + "py\nb\n" + BT
     + "\nC\n" + BT + "py\nc\n" + BT + "\nend\n",
     False),
    ("genuinely_truncated_mid_fence",
     "Sure, here is the code:\n" + BT + "python\ndef f():\n    return ",
     True),
    ("truncated_after_one_complete_block",
     "First:\n" + BT + "py\na=1\n" + BT + "\nSecond:\n" + BT + "py\ndef g(",
     True),
    ("no_fences_at_all",
     "Salting acts as a natural preservative that extends shelf life.\n",
     False),
    ("indented_closed_block",
     "1. Step one:\n\n    " + BT + "bash\n    ls -la\n    " + BT + "\n\n2. Step two.\n",
     False),
    ("indented_truncated_block",
     "1. Step one:\n\n    " + BT + "bash\n    ls -la\n",
     True),
    ("inline_single_backticks_only",
     "Use `git status` and `ls` to check the tree. No code block here.\n",
     False),
    ("inline_triple_backticks_plus_closed_block",
     "The delimiter is " + BT + " (three backticks). Example:\n"
     + BT + "py\nx=1\n" + BT + "\nDone.\n",
     False),
    ("four_backtick_wrapper_closed",
     "Nested:\n" + BT4 + "\n" + BT + "py\nx=1\n" + BT + "\n" + BT4 + "\nend.\n",
     False),
]

BALANCED = [(n, t) for n, t, e in CASES if e is False]
TRUNCATED = [(n, t) for n, t, e in CASES if e is True]


# ---------------------------------------------------------------------------
# 1. The replacement is correct on every case.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,text,expect", CASES, ids=[c[0] for c in CASES])
def test_fence_parity_verdict(name, text, expect):
    assert strategies.has_unclosed_code_block(text) is expect


def test_zero_fences_is_even_not_odd():
    """The boundary the parity rule has to get right on its own terms."""
    assert strategies.FENCE_LINE_RE.findall("plain prose, no code") == []
    assert strategies.has_unclosed_code_block("plain prose, no code") is False


@pytest.mark.parametrize("empty", ["", None])
def test_empty_input_is_not_truncated(empty):
    assert strategies.has_unclosed_code_block(empty) is False


def test_parity_is_actually_parity_not_a_fixed_answer():
    """Guards against a stub that always returns False -- add fences one at a time."""
    text = "intro\n"
    for i in range(1, 7):
        text += BT + "\n"
        assert strategies.has_unclosed_code_block(text) is (i % 2 == 1), \
            "after %d fence lines parity must be %s" % (i, i % 2 == 1)


# ---------------------------------------------------------------------------
# 2. NEGATIVE CONTROL -- the old pattern fails these same inputs.
#    If any of these ever passes, the "defect" was never real and every test
#    above is measuring nothing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,text", BALANCED, ids=[c[0] for c in BALANCED])
def test_old_pattern_false_positives_where_new_one_is_right(name, text):
    """Every balanced case the old regex got wrong, and the new one gets right."""
    if name in ("no_fences_at_all", "inline_single_backticks_only"):
        pytest.skip("no fence line present; the old regex was right on these two")
    assert old_says_truncated(text) is True, \
        "precondition: the OLD pattern must flag this well-formed answer"
    assert strategies.has_unclosed_code_block(text) is False


@pytest.mark.parametrize("name,text", TRUNCATED, ids=[c[0] for c in TRUNCATED])
def test_new_pattern_still_catches_genuine_truncation(name, text):
    """The fix must not buy its precision by giving up recall."""
    assert strategies.has_unclosed_code_block(text) is True


def test_old_patterns_one_true_negative_was_an_artifact_of_no_trailing_newline():
    """Sharper than the packet: the sole balanced case the old regex got right
    only held because the probe string stopped exactly at the closing fence.

    Add the trailing newline that a real response almost always has, and the old
    regex flags it too. Measured on Bifrost 2026-09-09 against the shipped object.
    """
    base = "Here you go:\n" + BT + "python\nprint(1)\n" + BT
    assert old_says_truncated(base) is False
    assert old_says_truncated(base + "\n") is True
    assert old_says_truncated(base + "\n\n") is True
    for variant in (base, base + "\n", base + "\n\n"):
        assert strategies.has_unclosed_code_block(variant) is False


# ---------------------------------------------------------------------------
# 3. Known limitation, pinned on the record rather than left implicit.
# ---------------------------------------------------------------------------

def test_tilde_fences_are_not_counted_unchanged_from_before():
    """`~~~` fences are out of scope for BOTH implementations.

    A truncated tilde block was missed before this change and is still missed --
    no regression, but it is a real gap, so it is asserted rather than assumed.
    """
    closed = "Config:\n" + TL + "yaml\nk: v\n" + TL + "\nThat is it.\n"
    truncated = "Config:\n" + TL + "yaml\nk: v\n"
    assert old_says_truncated(truncated) is False, "old missed it too"
    assert strategies.has_unclosed_code_block(truncated) is False, "still missed"
    assert strategies.has_unclosed_code_block(closed) is False


# ---------------------------------------------------------------------------
# 4. The LIVE gate, not just the helper. This is what actually costs money.
# ---------------------------------------------------------------------------

class _Settings:
    """Stand-in pinned to values that isolate the truncation test.

    min_tokens and min_response_ratio are set so they CANNOT fire, otherwise a
    green result here could come from a different reason entirely.
    """
    confidence_gate_enabled = True
    confidence_min_tokens = 1
    confidence_min_response_ratio = 0.0
    confidence_truncation_penalty = True
    confidence_hedging_penalty = True


@pytest.fixture
def gate_armed(monkeypatch):
    monkeypatch.setattr(strategies, "settings", _Settings())
    # Positive control: with these settings the gate MUST still escalate a
    # genuinely truncated draft, and for this reason. Without this assertion a
    # suite in which the truncation test had simply been deleted would pass.
    escalate, reason = strategies.confidence_gate(
        "Here is the code:\n" + BT + "python\ndef f(",
        "write a function",
        ComplexityBand.COMPLEX,
    )
    assert escalate is True and "truncated_code_block" in reason, \
        "fixture must leave the truncation test REACHABLE, else every case is vacuous"
    return None


@pytest.mark.parametrize("name,text", BALANCED, ids=[c[0] for c in BALANCED])
def test_gate_does_not_escalate_well_formed_output(gate_armed, name, text):
    escalate, reason = strategies.confidence_gate(text, "explain this", ComplexityBand.COMPLEX)
    assert "truncated_code_block" not in reason, \
        "well-formed output must not be escalated as truncated (reason=%r)" % reason
    assert escalate is False, "no other gate reason should fire either (reason=%r)" % reason


@pytest.mark.parametrize("name,text", TRUNCATED, ids=[c[0] for c in TRUNCATED])
def test_gate_still_escalates_genuine_truncation(gate_armed, name, text):
    escalate, reason = strategies.confidence_gate(text, "explain this", ComplexityBand.COMPLEX)
    assert escalate is True
    assert "truncated_code_block" in reason


def test_gate_respects_the_truncation_penalty_switch(monkeypatch):
    """The opt-out must still work after the rewrite."""
    class Off(_Settings):
        confidence_truncation_penalty = False

    monkeypatch.setattr(strategies, "settings", Off())
    _, reason = strategies.confidence_gate(
        "Here is the code:\n" + BT + "python\ndef f(", "write a function",
        ComplexityBand.COMPLEX)
    assert "truncated_code_block" not in reason


# ---------------------------------------------------------------------------
# 5. Source-level shape: prove the edit landed in the file, not just in memory.
# ---------------------------------------------------------------------------

def test_old_symbol_is_gone_and_new_ones_are_present():
    src = STRATEGIES.read_text(encoding="utf-8")
    assert "UNCLOSED_BLOCK_RE" not in src, \
        "the inverted pattern's name must not survive anywhere in strategies.py"
    assert src.count("def has_unclosed_code_block") == 1
    assert src.count("has_unclosed_code_block(draft_text)") == 1, \
        "exactly one call site, inside confidence_gate"
    assert "FENCE_LINE_RE" in src


def test_old_pattern_literal_matches_the_bytes_that_actually_shipped():
    """Anchor the negative control to real bytes, not to a retyped recollection.

    OLD_PATTERN above is typed out by hand, so on its own it proves only that *a*
    regex of that shape misbehaves. This ties it to the line that was genuinely in
    strategies.py before the 2026-09-09 edit, read from the pre-edit backup. Without
    this, every negative-control assertion in this file rests on my transcription.
    """
    assert OLD_PATTERN.pattern == BT + r"[\w]*\n(?!.*" + BT + ")"
    assert OLD_PATTERN.flags == re.DOTALL | re.UNICODE == 48, \
        "shipped flags were measured as 48 on Bifrost 2026-09-09"

    bak = REPO / "strategies.py.bak-2026-09-09-truncation"
    if not bak.exists():
        pytest.skip("pre-edit backup absent on this checkout; byte anchor unavailable")
    shipped_line = ('UNCLOSED_BLOCK_RE = re.compile(r"' + BT + r"[\w]*\n(?!.*" + BT
                    + ')", re.DOTALL)')
    src = bak.read_text(encoding="utf-8")
    assert shipped_line in src, \
        "the backup must contain verbatim the pattern this file reconstructs"
