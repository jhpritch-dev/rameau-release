"""Coverage for reasoning_coalesce's TOKEN-ACCOUNTING branches.

These exist because the rest of the suite is blind to them. `<think>` blocks and side-channel
fields announce themselves and are covered by test_reasoning_inline_think.py; reasoning that
arrives as PLAIN PROSE in content announces nothing, and is detected only from
`usage.completion_tokens_details.reasoning_tokens`. Before this file there was no
`reasoning_tokens` fixture anywhere in tests/, so both branches could have been dead code and
every suite run would still have been green.

What these tests assert, deliberately:
  - the structured outcome -- markers, `finish_reason`, content -- never a message string. The
    L5 assertions in this repo have now been re-pinned to message prose twice; see
    tests/test_l5_fence_fallback.py.
  - for the reasoning-dominant branch, which by design writes NOTHING to the response, the LOG
    RECORD. Asserting only that a marker key is absent would pass identically if the branch
    never ran at all, which is the whole failure this file exists to prevent. The assertion is
    on the record's presence and its numbers, not on the sentence.
  - that the quiet cases stay quiet. A detector that fires on everything is not a detector.
"""
import logging

import pytest

from reasoning_coalesce import coalesce_reasoning_into_content

ENV_RATIO = "BIFROST_REASONING_DOMINANT_RATIO"
LOGGER = "bifrost.reasoning_coalesce"


def make(content, *, ct=0, rt=0, finish="stop", usage=True, side=None):
    """An openai-shaped response. `side` is a (field, value) side-channel pair."""
    msg = {"role": "assistant", "content": content}
    if side:
        msg[side[0]] = side[1]
    r = {"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage:
        r["usage"] = {"prompt_tokens": 10, "completion_tokens": ct,
                      "completion_tokens_details": {"reasoning_tokens": rt}}
    return r


def finish_of(r):
    return r["choices"][0].get("finish_reason")


def content_of(r):
    return r["choices"][0]["message"]["content"]


def dominant_records(caplog):
    """The reasoning-dominant log lines. Matched on the one stable token in the message."""
    return [r for r in caplog.records if "reasoning-dominant" in r.getMessage()]


@pytest.fixture
def at_info(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


# --------------------------------------------------------------------------------------------
# starved: an empty 200 the provider called a COMPLETION after billing reasoning tokens.
# This branch DOES alter the response, so it declares itself to the caller.
# --------------------------------------------------------------------------------------------

def test_empty_content_with_reasoning_tokens_is_reported_as_truncation():
    """The laguna-s-2.1 shape recorded in free_tier_budget.py:190-194 -- 179 reasoning tokens,
    0 chars, finish_reason "stop". It was counted internally and never reported to the caller,
    who saw a successful empty answer."""
    r = coalesce_reasoning_into_content(make("", ct=179, rt=179))
    assert r.get("_bifrost_reasoning_starved") is True
    assert finish_of(r) == "length"
    assert r["choices"][0].get("_bifrost_original_finish_reason") == "stop"


def test_starved_does_not_invent_an_original_finish_reason():
    """The provider already said "length", so there is nothing to correct and no original to
    preserve -- only the shape is reported."""
    r = coalesce_reasoning_into_content(make("", ct=50, rt=50, finish="length"))
    assert r.get("_bifrost_reasoning_starved") is True
    assert finish_of(r) == "length"
    assert "_bifrost_original_finish_reason" not in r["choices"][0]


def test_legitimate_empty_with_no_reasoning_billed_stays_a_completion():
    """No reasoning tokens means no evidence the answer was cut off. An empty completion is a
    real outcome and must not be relabelled."""
    r = coalesce_reasoning_into_content(make("", ct=0, rt=0))
    assert "_bifrost_reasoning_starved" not in r
    assert finish_of(r) == "stop"


def test_side_channel_promotion_wins_over_the_starved_path():
    """Promotion happens first and produces a real answer, so the response is not starved."""
    r = coalesce_reasoning_into_content(
        make("", ct=179, rt=179, side=("reasoning_content", "chain of thought")))
    assert r.get("_bifrost_reasoning_promoted") is True
    assert content_of(r) == "chain of thought"
    assert "_bifrost_reasoning_starved" not in r
    assert finish_of(r) == "stop"


# --------------------------------------------------------------------------------------------
# dominant: most of what the caller paid for was reasoning. LOG ONLY -- the branch alters
# nothing, so by this module's rule it adds nothing to the payload.
# --------------------------------------------------------------------------------------------

def test_reasoning_dominant_response_is_logged_and_left_entirely_alone(at_info):
    """The shape measured live on 2026-10-03: 58 of 64 completion tokens were reasoning. There
    IS visible content, and where the reasoning ends cannot be known without a marker, so
    nothing is cut, finish_reason does not move, and nothing is added to the payload."""
    r = coalesce_reasoning_into_content(make("the answer", ct=64, rt=58))
    recs = dominant_records(at_info)
    assert len(recs) == 1, "the dominant branch did not run"
    assert recs[0].args[:2] == (58, 64)
    assert round(recs[0].args[2]) == 91
    assert content_of(r) == "the answer"
    assert finish_of(r) == "stop"
    # the payload is untouched: no marker, no ratio, nothing for a client to interpret
    assert set(r) == {"choices", "usage"}


def test_ratio_is_bounded_when_the_provider_does_not_fold_reasoning_into_completion(at_info):
    """Whether completion_tokens INCLUDES reasoning_tokens is provider-dependent. Under the
    non-inclusive reading a raw rt/ct exceeds 1, so the denominator is max(ct, rt)."""
    coalesce_reasoning_into_content(make("x", ct=6, rt=58))
    recs = dominant_records(at_info)
    assert len(recs) == 1
    assert recs[0].args[:2] == (58, 58), "denominator was not max(ct, rt)"
    assert round(recs[0].args[2]) == 100


def test_light_reasoning_is_not_flagged(at_info):
    """A detector that fires on every reasoning model is not a detector."""
    r = coalesce_reasoning_into_content(make("a real answer", ct=100, rt=10))
    assert dominant_records(at_info) == []
    assert finish_of(r) == "stop"


def test_no_usage_block_is_no_signal(at_info):
    """Absent usage must read as "nothing to say", never as a ratio of 0/0."""
    r = coalesce_reasoning_into_content(make("answer", usage=False))
    assert dominant_records(at_info) == []
    assert "_bifrost_reasoning_starved" not in r
    assert finish_of(r) == "stop"


# --------------------------------------------------------------------------------------------
# the threshold
# --------------------------------------------------------------------------------------------

def test_threshold_override_is_honoured(monkeypatch, at_info):
    monkeypatch.setenv(ENV_RATIO, "1.0")
    coalesce_reasoning_into_content(make("the answer", ct=64, rt=58))
    assert dominant_records(at_info) == [], "0.906 fired against a threshold of 1.0"


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-1", "1.5", ""])
def test_a_bad_threshold_falls_back_instead_of_disabling_the_branch(monkeypatch, at_info, bad):
    """A junk or out-of-range value must not silently switch detection off -- that is the
    failure mode where a knob set outside the observed distribution makes a feature inert and
    the suite still passes. 0 would flag every response reporting any reasoning at all."""
    monkeypatch.setenv(ENV_RATIO, bad)
    coalesce_reasoning_into_content(make("the answer", ct=64, rt=58))
    assert len(dominant_records(at_info)) == 1


# --------------------------------------------------------------------------------------------
# the token-accounting branches must not disturb the tag-based ones
# --------------------------------------------------------------------------------------------

def test_unclosed_think_still_reports_truncation():
    r = coalesce_reasoning_into_content(make("<think>cut off mid thou", ct=64, rt=58))
    assert r.get("_bifrost_reasoning_truncated") is True
    assert finish_of(r) == "length"


def test_closed_think_plus_answer_still_strips_to_the_answer():
    r = coalesce_reasoning_into_content(make("<think>working</think>  the answer", ct=64, rt=58))
    assert r.get("_bifrost_think_stripped") is True
    assert content_of(r) == "the answer"


def test_malformed_response_is_returned_untouched():
    """No choices, but a usage block that would otherwise trip the ratio branch."""
    src = {"usage": {"completion_tokens": 64,
                     "completion_tokens_details": {"reasoning_tokens": 58}}}
    r = coalesce_reasoning_into_content(src)
    assert r is src
    assert set(r) == {"usage"}


@pytest.mark.parametrize("usage", [
    None,                                              # key absent
    "not-a-dict",                                      # wrong type
    {"completion_tokens": "many",
     "completion_tokens_details": {"reasoning_tokens": "lots"}},   # unparsable
    {"completion_tokens": 64, "completion_tokens_details": None},  # details null
    {"completion_tokens": 64, "completion_tokens_details": "nope"},
])
def test_an_unusable_usage_block_never_raises(at_info, usage):
    """A response shaping helper on the dispatch path must not be the thing that 500s a
    request because a provider sent an odd usage block."""
    r = make("answer")
    if usage is None:
        r.pop("usage")
    else:
        r["usage"] = usage
    out = coalesce_reasoning_into_content(r)
    assert dominant_records(at_info) == []
    assert finish_of(out) == "stop"
