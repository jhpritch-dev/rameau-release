"""The NON-streaming half of the finish_reason=="length" exemption (2026-10-06).

WHAT WAS MISSING. test_openai_compat_empty_is_wedged.py builds its protective pairs carefully and
names three cases that "must come out the OTHER way": a streamed tool call, a non-streamed tool
call, and a truncated empty STREAM. The fourth cell of that grid -- a truncated empty
NON-STREAMED response -- has no test, and the non-streaming refusal test asserts on
finish_reason "stop" (the _resp default). So nothing in the suite pinned the behavior this file
fixes, and the asymmetry shipped: one response, 0 chars with finish_reason "length", DELIVERED at
stream=True and raising WedgedBackendError at stream=False.

Measured on 2026-10-06, four Lane C runs:
  WedgedBackendError .../api/chat qwen3.5-4b-32k: NO content at all (0 chars) and claimed 53
  generated tokens

WHY THE NEGATIVE CASES CARRY THE WEIGHT. The one-line fix a reader would reach for first is an
early `if finish_reason == "length": return None` at the top of wedge_reason. That passes every
empty-body assertion here and is WRONG: it would also excuse reserved-token spew, repetition and
the token-contradiction shape, turning a wedge detector off for every truncated response. So the
refusal cases are not decoration -- they are the discriminator, and they vary in CARDINALITY and
in WHICH ARM fires (reserved-token, repetition, token-contradiction), because a single-shape
fixture cannot tell a narrow fix from a blanket one.

Three levels are asserted, deliberately: wedge_reason (the predicate), check_openai_response (the
shaped-response reader that has to find finish_reason at all), and openai_compat's real
non-streaming entry point with httpx faked out (the path a caller travels).
"""
import asyncio
import json
import types

import httpx
import pytest

import degenerate as D
from backends import openai_compat as oc
from degenerate import WedgedBackendError

MSGS = [{"role": "user", "content": "hi"}]
USAGE = {"prompt_tokens": 5, "completion_tokens": 53, "total_tokens": 58}
USAGE_ZERO = {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}
TOOL_CALL = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]

# The three degenerate shapes, one per arm of wedge_reason. None may be excused by "length".
RESERVED = "<unused24>" * 31                 # reserved-token arm, ratio 1.0
REPETITION = "foo bar " * 15                 # repetition arm: 30 parts, 2 distinct
CONTRADICTION = "x" * 20 + " a real sentence of prose here."   # >= 40 chars, 0 tokens claimed


def _resp(message, finish="stop", usage=None):
    return {"id": "chatcmpl-t", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": USAGE if usage is None else usage}


# ---------------------------------------------------------------------------
# level 1 -- wedge_reason, the predicate
# ---------------------------------------------------------------------------

def test_predicate_empty_with_length_is_not_wedged():
    assert D.wedge_reason("", USAGE, empty_is_wedged=True, finish_reason="length") is None


def test_predicate_empty_with_stop_is_still_wedged():
    r = D.wedge_reason("", USAGE, empty_is_wedged=True, finish_reason="stop")
    assert r and "NO content at all" in r, r


def test_predicate_empty_with_absent_finish_reason_is_still_wedged():
    """An absent finish_reason must not read as "length". A backend that reports nothing has
    told us nothing, and the pre-2026-10-06 behavior is the right default there."""
    assert D.wedge_reason("", USAGE, empty_is_wedged=True) is not None
    assert D.wedge_reason("", USAGE, empty_is_wedged=True, finish_reason=None) is not None


@pytest.mark.parametrize("body,usage,arm", [
    (RESERVED, USAGE, "reserved-token"),
    (REPETITION, USAGE, "repetition"),
    (CONTRADICTION, USAGE_ZERO, "self-contradictory"),
])
def test_predicate_nonempty_degenerate_is_not_excused_by_length(body, usage, arm):
    """THE DISCRIMINATOR. degenerate.py's own streaming comment states the limit: "A NON-empty
    degenerate body is not excused by 'length'". A blanket early-return would fail all three."""
    r = D.wedge_reason(body, usage, empty_is_wedged=True, finish_reason="length")
    assert r is not None, "%s arm was wrongly excused by finish_reason=length" % arm
    assert arm in r, r


def test_predicate_tool_call_empty_is_unchanged():
    assert D.wedge_reason("", USAGE, has_tool_calls=True, empty_is_wedged=True,
                          finish_reason="tool_calls") is None
    assert D.wedge_reason("", USAGE, has_tool_calls=True, empty_is_wedged=True) is None


def test_predicate_opt_out_still_wins():
    """empty_is_wedged=False is the /api/generate FIM path. Unchanged either way."""
    assert D.wedge_reason("", USAGE, empty_is_wedged=False, finish_reason="length") is None
    assert D.wedge_reason("", USAGE, empty_is_wedged=False, finish_reason="stop") is None


# ---------------------------------------------------------------------------
# level 2 -- check_openai_response must FIND finish_reason on the shaped response
# ---------------------------------------------------------------------------

def test_shaped_reader_finds_finish_reason():
    assert D.finish_reason_of(_resp({"role": "assistant", "content": ""},
                                    finish="length")) == "length"


@pytest.mark.parametrize("bad", [None, {}, {"choices": []}, {"choices": [None]},
                                 "a bare string", 42, {"choices": [{"finish_reason": 7}]}])
def test_shaped_reader_never_raises(bad):
    """Same discipline as content_of and has_tool_calls: a detector that can throw on the hot
    path is worse than the bug it hunts. A non-string finish_reason reads as absent."""
    assert D.finish_reason_of(bad) is None


def test_shaped_empty_with_length_passes():
    D.check_openai_response(_resp({"role": "assistant", "content": ""}, finish="length"),
                            endpoint="t", model="m", empty_is_wedged=True)


def test_shaped_empty_with_stop_raises():
    with pytest.raises(WedgedBackendError):
        D.check_openai_response(_resp({"role": "assistant", "content": ""}, finish="stop"),
                                endpoint="t", model="m", empty_is_wedged=True)


def test_shaped_reserved_spew_with_length_still_raises():
    with pytest.raises(WedgedBackendError):
        D.check_openai_response(_resp({"role": "assistant", "content": RESERVED},
                                      finish="length"),
                                endpoint="t", model="m", empty_is_wedged=True)


# ---------------------------------------------------------------------------
# level 3 -- the real openai_compat non-streaming entry point
# ---------------------------------------------------------------------------

def _install_fake_http(monkeypatch, body):
    class _Resp:
        status_code = 200

        def json(self):
            return body

        @property
        def text(self):
            return json.dumps(body)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    monkeypatch.setattr(oc, "httpx", types.SimpleNamespace(
        AsyncClient=_Client, HTTPStatusError=httpx.HTTPStatusError))


def _run_request(monkeypatch, body):
    _install_fake_http(monkeypatch, body)
    return asyncio.run(oc.openai_compat_chat_completion(MSGS, provider="flm", stream=False))


def test_nonstreamed_empty_with_finish_length_is_delivered_not_refused(monkeypatch):
    """THE MISSING FOURTH CELL, and the live defect. Its streaming twin already exists as
    test_streamed_empty_with_finish_length_is_delivered_not_refused."""
    got = _run_request(monkeypatch, _resp({"role": "assistant", "content": ""},
                                          finish="length"))
    assert got["choices"][0]["finish_reason"] == "length", got
    assert got["usage"]["completion_tokens"] == 53, got


def test_nonstreamed_empty_with_finish_stop_is_still_refused(monkeypatch):
    """The protective pair. The existing suite's non-streaming refusal test is exactly this
    shape, and it must keep failing the backend."""
    with pytest.raises(WedgedBackendError):
        _run_request(monkeypatch, _resp({"role": "assistant", "content": ""}, finish="stop"))


def test_nonstreamed_repetition_with_length_is_still_refused(monkeypatch):
    with pytest.raises(WedgedBackendError):
        _run_request(monkeypatch, _resp({"role": "assistant", "content": REPETITION},
                                        finish="length"))


def test_nonstreamed_thinking_is_promoted_before_the_exemption_is_reached(monkeypatch):
    """ORDER MATTERS. coalesce_reasoning_into_content runs BEFORE the wedge check, so a thinking
    model whose output landed in the side channel gets PROMOTED and delivered as content -- the
    exemption never fires and the caller still gets the text. Asserting this keeps the fix from
    being read as "empty responses are fine now"."""
    got = _run_request(monkeypatch, _resp(
        {"role": "assistant", "content": "", "reasoning_content": "the actual answer"},
        finish="length"))
    assert got["choices"][0]["message"]["content"] == "the actual answer", got
    assert got.get("_bifrost_reasoning_promoted") is True, got
