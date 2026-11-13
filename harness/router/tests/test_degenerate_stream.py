"""#98 -- wedge detection must cover STREAMING responses too.

#87 (v341) put one wedge check at the backend layer and wired it into the non-streaming
paths only. The consequence was not merely "less coverage": the SAME wedged instance was
REFUSED when a caller passed stream=False and delivered verbatim when it passed stream=True.
Nothing about the backend differed between those two outcomes -- only the flag did.

WHAT IS ASSERTED, AND WHY IN PAIRS. Every test here reads what the CONSUMER receives, not
what the guard believes. And each protective claim is paired with a case that must come out
the other way: a guard that held everything would fail the pass-through tests, and a guard
that held nothing would fail the refusal tests. Neither degenerate implementation can make
this file green.
"""
import asyncio
import json
import logging

import pytest

import degenerate
from degenerate import WedgedBackendError, guard_stream


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sse(text=None, usage=None, role=False, done=False):
    """One SSE chunk in the shape the backends actually emit."""
    if done:
        return "data: [DONE]\n\n"
    delta = {"role": "assistant"} if role else ({"content": text} if text else {})
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk", "model": "m",
           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    if usage is not None:
        obj["usage"] = usage
    return "data: %s\n\n" % json.dumps(obj)


def _text_sse(text):
    """The /api/generate shape: choices[0].text, no delta. Covered because
    ollama_completion streams through the same guard."""
    obj = {"id": "cmpl-t", "object": "text_completion", "model": "m",
           "choices": [{"index": 0, "text": text, "finish_reason": None}]}
    return "data: %s\n\n" % json.dumps(obj)


async def _aiter(items):
    for i in items:
        yield i


async def _drain(chunks):
    out = []
    async for c in guard_stream(_aiter(chunks), endpoint="http://test/x", model="m"):
        out.append(c)
    return out


# The measured 2026-08-07 signature: a tier-2 leg returned "<unused24>" x31 and nothing else.
WEDGE_CHUNKS = [_sse(role=True)] + [_sse("<unused24>") for _ in range(31)] + [_sse(done=True)]
PROSE = ["The ", "quick ", "brown ", "fox ", "jumps ", "over ", "the ", "lazy ", "dog."]
PROSE_CHUNKS = [_sse(role=True)] + [_sse(w) for w in PROSE] + [_sse(done=True)]


# ---------------------------------------------------------------------------
# the protective claim: a wedged stream reaches the client as NOTHING
# ---------------------------------------------------------------------------

def test_reserved_token_stream_is_refused_with_zero_bytes_delivered():
    asyncio.run(_test_reserved_token_stream_is_refused_with_zero_bytes_delivered())


async def _test_reserved_token_stream_is_refused_with_zero_bytes_delivered():
    out = []
    with pytest.raises(WedgedBackendError):
        async for c in guard_stream(_aiter(WEDGE_CHUNKS), endpoint="http://test/x", model="m"):
            out.append(c)
    assert out == [], (
        "the client received %d chunk(s) of a wedged stream -- a refusal that arrives after "
        "the garbage does is not a refusal" % len(out))


def test_repetition_stream_is_refused_with_zero_bytes_delivered():
    asyncio.run(_test_repetition_stream_is_refused_with_zero_bytes_delivered())


async def _test_repetition_stream_is_refused_with_zero_bytes_delivered():
    """The second wedge shape: a couple of fragments repeated to fill the response. It can
    only be recognized after ~20 words, which is exactly why the hold is expressed as
    'still consistent with a wedge' rather than 'first N characters'."""
    chunks = [_sse(role=True)] + [_sse("loop ") for _ in range(25)] + [_sse(done=True)]
    out = []
    with pytest.raises(WedgedBackendError):
        async for c in guard_stream(_aiter(chunks), endpoint="http://test/x", model="m"):
            out.append(c)
    assert out == [], "a repetition wedge leaked %d chunk(s) to the client" % len(out)


def test_generate_shape_is_guarded_too():
    asyncio.run(_test_generate_shape_is_guarded_too())


async def _test_generate_shape_is_guarded_too():
    """/api/generate carries its text as choices[0].text, not as a delta. Reading only the
    delta would leave the FIM/autocomplete path silently unguarded while the suite stayed
    green -- the same class of gap as #98 itself."""
    chunks = [_text_sse("<unused24>") for _ in range(31)]
    out = []
    with pytest.raises(WedgedBackendError):
        async for c in guard_stream(_aiter(chunks), endpoint="http://test/x", model="m"):
            out.append(c)
    assert out == []


# ---------------------------------------------------------------------------
# the paired claim: a healthy stream is UNCHANGED, and barely delayed
# ---------------------------------------------------------------------------

def test_healthy_stream_passes_through_byte_for_byte():
    asyncio.run(_test_healthy_stream_passes_through_byte_for_byte())


async def _test_healthy_stream_passes_through_byte_for_byte():
    out = await _drain(PROSE_CHUNKS)
    assert out == PROSE_CHUNKS, "the guard altered, dropped or reordered a healthy stream"


def test_healthy_stream_is_released_within_a_few_tokens():
    asyncio.run(_test_healthy_stream_is_released_within_a_few_tokens())


async def _test_healthy_stream_is_released_within_a_few_tokens():
    """The design claim under test. The guard is only acceptable if the hold on a GOOD
    answer is negligible; if it were not, the honest fix would have been to leave streaming
    unguarded and say so. Measured: released after the role chunk plus 3 words."""
    consumed = 0

    async def counting():
        nonlocal consumed
        for c in PROSE_CHUNKS:
            consumed += 1
            yield c

    first_at = None
    async for _ in guard_stream(counting(), endpoint="http://test/x", model="m"):
        first_at = consumed
        break
    assert first_at is not None, "the guard never emitted anything for a healthy stream"
    assert first_at <= 5, (
        "first chunk reached the client only after %d source chunks -- the hold is no longer "
        "a few tokens" % first_at)


def test_a_special_token_prefix_does_not_condemn_a_real_answer():
    asyncio.run(_test_a_special_token_prefix_does_not_condemn_a_real_answer())


async def _test_a_special_token_prefix_does_not_condemn_a_real_answer():
    """The false positive that judging PARTIAL text creates, and the reason for
    STREAM_MIN_EVIDENCE_CHARS.

    An answer that opens by quoting a chat-template token is 100% reserved vocabulary for
    exactly one chunk. The complete-response check never sees that moment; a streaming guard
    does, and an earlier draft of this guard refused it -- destroying a perfectly good answer
    to prevent a wedge that was not there.
    """
    chunks = ([_sse("<|im_start|>"), _sse(" marks "), _sse("the "), _sse("start "),
               _sse("of "), _sse("a "), _sse("turn."), _sse(done=True)])
    out = await _drain(chunks)
    assert out == chunks, (
        "a real answer beginning with a special token was withheld or refused")


def test_a_short_complete_wedge_is_refused_exactly_as_a_non_streamed_one_is():
    asyncio.run(_test_a_short_complete_wedge_is_refused_exactly_as_a_non_streamed_one_is())


async def _test_a_short_complete_wedge_is_refused_exactly_as_a_non_streamed_one_is():
    """The other side of that floor. Waiting for more evidence must not become leniency: once
    the stream ENDS, the held text is a complete body and is judged by the ordinary check.
    Otherwise a short all-reserved answer would be refused at stream=False and delivered at
    stream=True -- #98 again, in miniature, introduced by its own fix."""
    chunks = [_sse("<unused24>"), _sse(done=True)]
    out = []
    with pytest.raises(WedgedBackendError):
        async for c in guard_stream(_aiter(chunks), endpoint="http://test/x", model="m"):
            out.append(c)
    assert out == []


def test_unparseable_chunks_pass_through():
    asyncio.run(_test_unparseable_chunks_pass_through())


async def _test_unparseable_chunks_pass_through():
    """A detector that throws on the hot path is worse than the bug it hunts."""
    chunks = ["data: not-json\n\n", ": keepalive\n\n", _sse("Hello "), _sse("there "),
              _sse("world."), _sse(done=True)]
    out = await _drain(chunks)
    assert out == chunks


# ---------------------------------------------------------------------------
# the honest limitation: past the hold, the guard can only RECORD
# ---------------------------------------------------------------------------

def test_mid_stream_degeneration_is_recorded_but_not_refused(caplog):
    with caplog.at_level(logging.ERROR, logger="bifrost.degenerate"):
        out = asyncio.run(_mid_stream_degeneration())
    assert out is not None and len(out) > 0, "the stream was refused after delivery began"
    assert any("WEDGED BACKEND" in r.getMessage() for r in caplog.records), (
        "a stream that degenerated after the hold was released produced NO evidence -- "
        "silently passing it on is the failure this module exists to prevent")


async def _mid_stream_degeneration():
    """A stream that opens as real prose and turns to reserved tokens is already past the
    hold. It CANNOT be refused -- those bytes are gone -- so the guard logs and counts it
    instead. Asserting a refusal here would be asserting something impossible."""
    chunks = ([_sse(role=True)] + [_sse(w) for w in PROSE[:4]]
              + [_sse("<unused24>") for _ in range(60)] + [_sse(done=True)])
    return await _drain(chunks)


def test_zero_completion_tokens_at_end_of_stream_is_recorded(caplog):
    with caplog.at_level(logging.ERROR, logger="bifrost.degenerate"):
        out = asyncio.run(_zero_completion_tokens())
    assert out is not None and len(out) > 0
    assert any("0 generated tokens" in r.getMessage() for r in caplog.records), (
        "usage arrives in the FINAL chunk, so the token-count contradiction is only "
        "visible at end-of-stream; not reading it there discards the second signal entirely")


async def _zero_completion_tokens():
    body = "This body is comfortably longer than the forty-character floor for the check. "
    chunks = ([_sse(role=True), _sse(body),
               _sse(usage={"prompt_tokens": 11, "completion_tokens": 0}), _sse(done=True)])
    return await _drain(chunks)


def test_a_clean_stream_logs_nothing(caplog):
    """CONTROL for the two tests above. If the end-of-stream check fired on healthy output,
    their green results would mean nothing."""
    with caplog.at_level(logging.ERROR, logger="bifrost.degenerate"):
        out = asyncio.run(_drain(PROSE_CHUNKS))
    assert out == PROSE_CHUNKS
    assert not [r for r in caplog.records if "WEDGED BACKEND" in r.getMessage()], (
        "the guard reported a healthy stream as wedged")


# ---------------------------------------------------------------------------
# the off-switch keeps the evidence
# ---------------------------------------------------------------------------

def test_action_log_delivers_the_stream_but_still_records(caplog, monkeypatch):
    monkeypatch.setattr(degenerate, "WEDGE_ACTION", "log")
    with caplog.at_level(logging.ERROR, logger="bifrost.degenerate"):
        out = asyncio.run(_drain(WEDGE_CHUNKS))
    assert out == WEDGE_CHUNKS, (
        "BIFROST_WEDGE_ACTION=log must change behavior back to pass-through, not "
        "half-refuse -- an operator turning the refusal off gets the old behavior exactly")
    assert any("WEDGED BACKEND" in r.getMessage() for r in caplog.records), (
        "the off-switch silenced the evidence as well as the refusal")


# ---------------------------------------------------------------------------
# WIRING. A correct guard nothing calls is exactly how #98 came to exist.
# ---------------------------------------------------------------------------
#
# #87 wrote one good check and wired it into some of the paths. Every unit test above would
# have passed on the day #98 was filed. So these four drive the REAL entry points -- one per
# streaming shape the Router can return -- and a backend that forgets the guard fails here
# rather than shipping.

async def _drain_entry(coro):
    out = []
    async for c in await coro:
        out.append(c)
    return out


def _a_tier():
    from config import TIER_BACKENDS
    return next(iter(TIER_BACKENDS))


def test_wiring_ollama_native_chat_stream(monkeypatch):
    from backends import ollama as ob

    async def fake(url, payload):
        for c in WEDGE_CHUNKS:
            yield c

    monkeypatch.setattr(ob, "_stream_ollama_native_chat", fake)
    with pytest.raises(WedgedBackendError):
        asyncio.run(_drain_entry(ob.ollama_chat_completion(
            [{"role": "user", "content": "hi"}], _a_tier(), stream=True)))


def test_wiring_ollama_v1_passthrough_stream(monkeypatch):
    """The tools branch. It stays on the /v1 passthrough for an unrelated reason (the native
    translator does not relay tool_calls), which makes it the easiest one to leave behind."""
    from backends import ollama as ob

    async def fake(url, payload):
        for c in WEDGE_CHUNKS:
            yield c

    monkeypatch.setattr(ob, "_stream_ollama", fake)
    with pytest.raises(WedgedBackendError):
        asyncio.run(_drain_entry(ob.ollama_chat_completion(
            [{"role": "user", "content": "hi"}], _a_tier(), stream=True,
            tools=[{"type": "function", "function": {"name": "f"}}])))


def test_wiring_ollama_generate_stream(monkeypatch):
    from backends import ollama as ob

    async def fake(url, payload, model):
        for c in [_text_sse("<unused24>") for _ in range(31)]:
            yield c

    monkeypatch.setattr(ob, "_stream_ollama_generate", fake)
    with pytest.raises(WedgedBackendError):
        asyncio.run(_drain_entry(ob.ollama_completion("def f(", _a_tier(), stream=True)))


def test_wiring_openai_compat_stream(monkeypatch):
    """provider=flm is the NPU tier; it also happens to be the one provider with no API key,
    so this exercises the wiring without needing credentials."""
    from backends import openai_compat as oc

    async def fake(url, headers, payload, timeout):
        for c in WEDGE_CHUNKS:
            yield c

    monkeypatch.setattr(oc, "_stream", fake)
    with pytest.raises(WedgedBackendError):
        asyncio.run(_drain_entry(oc.openai_compat_chat_completion(
            [{"role": "user", "content": "hi"}], provider="flm", stream=True)))
