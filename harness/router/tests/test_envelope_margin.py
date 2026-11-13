"""#152 -- the envelope gate is a cliff; this is the guardrail before it.

The gate in subtask_graph FAILS a leaf leg ("re-decompose smaller") rather than truncating.
Until now nothing warned as you approached it: #151 added 322 bytes of prompt text and the
first signal was a test_dep_context failure whose message blames truncation for being "too
generous", which is exactly the wrong place to look.

These tests answer the question an editor actually has -- *how much room is left?* -- and
they answer it in characters, because characters are what an editor adds.

They also keep the cost of the single global _SYSTEM_PROMPT_RESERVE_TOKENS visible. Every
leg pays the reserve sized for the largest consumer, so a `docs` leg surrenders upstream
context to cover a test-leg prompt it never receives. autopilot_graph says the per-kind
reserve is the right fix and has deferred it six times; the report below is printed on every
run so the number stops being a comment nobody re-derives.
"""
import pytest

from bifrost_message import SubtaskSpec
from subtask_graph import (
    _build_messages,
    _CHARS_PER_TOKEN,
    _ENVELOPE_MARGIN_TOKENS,
    _estimate_envelope_tokens,
    _OUTPUT_RESERVE_TOKENS,
)
from autopilot_graph import _SYSTEM_PROMPT_RESERVE_TOKENS

from tests.test_dep_context import _res, _spec, _with_dep_context

# Large enough that the dep-context allowance is saturated and truncation fills it: this is
# the worst case the gate ever sees, and the only one where headroom is meaningful.
_SATURATING_UPSTREAM = 50_000

FILE_DESC = ("Write a command-line tool that reads a CSV file and prints summary statistics "
             "for each numeric column")


def _worst_case_headroom():
    s = _with_dep_context(_spec(["s1"]), {"s1": _res("s1", "x" * _SATURATING_UPSTREAM)})
    envelope = _estimate_envelope_tokens(_build_messages(s, [], []))
    limit = s.context_budget_tokens - _OUTPUT_RESERVE_TOKENS
    return envelope, limit, limit - envelope


def _system_tokens(deliverable):
    spec = SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s1", description=FILE_DESC,
        acceptance_criteria=["ac"], assigned_tier="2_qwen_coder", deliverable=deliverable,
    )
    msgs = [m for m in _build_messages(spec, {}) if m.get("role") == "system"]
    return _estimate_envelope_tokens(msgs), sum(len(m.get("content", "")) for m in msgs)


def test_envelope_keeps_a_margin_above_the_gate():
    """THE alarm. Fails while there is still room, not after legs start failing.

    If this fails, you have spent the prompt budget. The options, in order of preference:
      1. shorten what you just added (the block is load-bearing but rarely minimal);
      2. give the deliverable kinds their own reserves -- see the report test below;
      3. raise _SYSTEM_PROMPT_RESERVE_TOKENS again, which is a 1:1 trade against how much
         upstream context every leg may carry.
    Do NOT lower _ENVELOPE_MARGIN_TOKENS to make this pass. That deletes the alarm.
    """
    envelope, limit, headroom = _worst_case_headroom()
    assert headroom >= _ENVELOPE_MARGIN_TOKENS, (
        "envelope %d tok vs limit %d leaves %d tok headroom, inside the %d tok margin. "
        "You may add about %d more characters of prompt before leaf legs start FAILING "
        "the gate outright."
        % (envelope, limit, headroom, _ENVELOPE_MARGIN_TOKENS,
           max(0, int(headroom * _CHARS_PER_TOKEN)))
    )


def test_the_gate_itself_is_not_already_breached():
    """The hard invariant, stated where an editor will look for it.

    test_dep_context asserts the same thing from the truncation side; this one says it in
    the vocabulary of whoever just edited a prompt.
    """
    envelope, limit, headroom = _worst_case_headroom()
    assert headroom >= 0, (
        "envelope %d tok EXCEEDS the %d tok limit by %d -- leaf legs are failing the gate "
        "right now, before any model is called" % (envelope, limit, -headroom)
    )


def test_the_reserve_covers_the_largest_system_prompt():
    """_SYSTEM_PROMPT_RESERVE_TOKENS is what the dep-context allowance subtracts.

    If a system prompt outgrows it, the allowance over-grants and the overflow lands on the
    gate -- which is how the cliff gets reached without anyone touching the gate.
    """
    worst_kind, worst_tok = max(
        ((d, _system_tokens(d)[0]) for d in ("tests", "code_selftest", "code", "docs")),
        key=lambda kv: kv[1],
    )
    assert worst_tok <= _SYSTEM_PROMPT_RESERVE_TOKENS, (
        "the %s system prompt is %d tok but the allowance reserves only %d -- dependency "
        "context is being over-granted by %d tok, and the excess overflows onto the gate"
        % (worst_kind, worst_tok, _SYSTEM_PROMPT_RESERVE_TOKENS,
           worst_tok - _SYSTEM_PROMPT_RESERVE_TOKENS)
    )


def test_report_the_cost_of_a_single_global_reserve(capsys):
    """Not a pass/fail claim -- a standing measurement.

    autopilot_graph:2752 records that a per-kind reserve is the right fix and that it has
    been deferred repeatedly because the cost is "invisible in production". Print it.
    """
    rows = []
    for d in ("tests", "code_selftest", "code", "docs"):
        tok, chars = _system_tokens(d)
        rows.append((d, chars, tok, _SYSTEM_PROMPT_RESERVE_TOKENS - tok))

    envelope, limit, headroom = _worst_case_headroom()
    with capsys.disabled():
        print("\n  envelope %d / limit %d -> headroom %+d tok (margin %d, ~%d chars spare)"
              % (envelope, limit, headroom, _ENVELOPE_MARGIN_TOKENS,
                 max(0, int(headroom * _CHARS_PER_TOKEN))))
        print("  reserve %d tok charged to EVERY leg; per-kind actual use:"
              % _SYSTEM_PROMPT_RESERVE_TOKENS)
        for d, chars, tok, waste in rows:
            print("    %-14s %6d chars %5d tok   overpaid by %5d tok (~%d chars of "
                  "upstream context)" % (d, chars, tok, waste, int(waste * _CHARS_PER_TOKEN)))

    # The report is the point, but assert the shape so it cannot silently stop working.
    assert len(rows) == 4
    assert all(tok > 0 for _d, _c, tok, _w in rows)


@pytest.mark.parametrize("deliverable", ["tests", "code_selftest", "code", "docs"])
def test_no_kind_individually_breaches_the_reserve(deliverable):
    tok, chars = _system_tokens(deliverable)
    assert tok <= _SYSTEM_PROMPT_RESERVE_TOKENS, (
        "%s system prompt is %d chars = %d tok, over the %d tok reserve"
        % (deliverable, chars, tok, _SYSTEM_PROMPT_RESERVE_TOKENS)
    )
