"""Option (b): when a stream truncates MID-THINKING, the caller gets EMPTY content with
finish_reason "length" intact -- not the chain-of-thought dressed up as an answer.

MEASURED 2026-10-05 against the live Router, tier 3-lite-free via X-Tier-Override. The upstream
provider streams the thinking incrementally in `delta.reasoning` with `delta.content` EMPTY, and
then, when the budget runs out mid-thought, emits ONE content delta that is BYTE-IDENTICAL to the
accumulated reasoning (208/208 chars at max_tokens=60, 586/586 at 160) and reads
"Here's a thinking process: 1. **Analyze User Input:** ...". finish_reason "length" arrives in the
NEXT chunk, not that one -- so the content chunk has to be held until finish is known, which is
why this lives in guard_stream (it already holds) rather than in a backend.

Nothing in the Router coalesces on stream=True: openai_compat.py:254 and main.py:3401/:3497 are
all in the non-streaming branch. So this shape reached callers verbatim.

WHAT IS ASSERTED, AND WHY IN PAIRS. The protective claim -- content emptied -- is paired with four
cases that must come out the OTHER way. An implementation that empties content whenever
finish_reason is "length" would fail test 2 and 4; one that never empties fails test 1; one that
ignores the length threshold fails test 6. No degenerate implementation makes this file green.
"""
import asyncio
import json

from degenerate import guard_stream


# ---------------------------------------------------------------------------
# helpers -- the shape the provider actually emits (measured, see docstring)
# ---------------------------------------------------------------------------

def _sse(content=None, reasoning=None, role=False, finish=None, usage=None, done=False):
    if done:
        return "data: [DONE]\n\n"
    delta = {}
    if role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    obj = {"id": "chatcmpl-t", "object": "chat.completion.chunk", "model": "m",
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return "data: %s\n\n" % json.dumps(obj)


async def _aiter(items):
    for i in items:
        yield i


async def _drain(chunks):
    out = []
    async for c in guard_stream(_aiter(chunks), endpoint="http://test/x", model="m"):
        out.append(c)
    return out


def _run(chunks):
    return asyncio.run(_drain(chunks))


def _contents(out):
    """Every delta.content the CONSUMER receives, in order. Reads what arrives, not what the
    guard believes."""
    vals = []
    for c in out:
        body = c[5:].strip()
        if not body or body == "[DONE]":
            continue
        d = (json.loads(body).get("choices") or [{}])[0].get("delta") or {}
        if "content" in d:
            vals.append(d["content"])
    return vals


def _finishes(out):
    fs = []
    for c in out:
        body = c[5:].strip()
        if not body or body == "[DONE]":
            continue
        fr = (json.loads(body).get("choices") or [{}])[0].get("finish_reason")
        if fr:
            fs.append(fr)
    return fs


def _has_usage(out):
    for c in out:
        body = c[5:].strip()
        if not body or body == "[DONE]":
            continue
        if json.loads(body).get("usage"):
            return True
    return False


# The real signature: thinking streamed in reasoning, then one content delta equal to all of it.
COT = ("Here's a thinking process:\n\n1.  **Analyze User Input:**\n   - User asks: "
       "\"Name three primary colors.\"\n   - The")
COT_FRAGMENTS = [COT[i:i + 8] for i in range(0, len(COT), 8)]
USAGE = {"prompt_tokens": 12, "completion_tokens": 60, "total_tokens": 72}


def _cot_stream(final_content, finish="length"):
    return (
        [_sse(role=True)]
        + [_sse(content="", reasoning=f) for f in COT_FRAGMENTS]
        + [_sse(content=final_content)]
        + [_sse(finish=finish)]
        + [_sse(finish=finish, usage=USAGE)]
        + [_sse(done=True)]
    )


# ---------------------------------------------------------------------------
# 1. THE PROTECTIVE CLAIM
# ---------------------------------------------------------------------------

def test_cot_as_content_at_truncation_is_delivered_with_empty_content():
    out = _run(_cot_stream(COT))

    # The chain-of-thought must not reach the caller as the answer.
    assert COT not in "".join(_contents(out)), "the CoT was delivered as content"
    assert all(v == "" for v in _contents(out)), _contents(out)

    # ...and option (b) is EMPTY CONTENT WITH THE METADATA INTACT, not a dropped stream.
    assert "length" in _finishes(out), "finish_reason=length was lost"
    assert _has_usage(out), "usage was lost"
    assert any(c.strip() == "data: [DONE]" for c in out), "[DONE] was lost"


# ---------------------------------------------------------------------------
# 2-5. THE PAIRS -- each must come out the OTHER way
# ---------------------------------------------------------------------------

def test_a_genuinely_truncated_answer_is_not_emptied():
    """finish_reason=length alone must NOT empty content. An answer that was cut off is still an
    answer, and emptying it would destroy real output -- the failure mode this change could
    most easily introduce."""
    answer = "The three primary colors are red, blue and"
    out = _run(_cot_stream(answer))
    assert answer in "".join(_contents(out)), _contents(out)
    assert "length" in _finishes(out)


def test_normal_completion_with_separate_cot_is_untouched():
    """At finish_reason=stop the content is a real answer and the reasoning is separate CoT
    (measured: 257 vs 1,245 chars). Nothing should change."""
    answer = "Red, blue and yellow."
    out = _run(_cot_stream(answer, finish="stop"))
    assert answer in "".join(_contents(out)), _contents(out)
    assert "stop" in _finishes(out)


def test_truncation_with_no_reasoning_at_all_is_untouched():
    """Ordinary truncation on a non-thinking model: no side channel exists, so there is nothing
    to compare against and content must pass through."""
    body = "A truncated sentence that just stops right"
    chunks = ([_sse(role=True)] + [_sse(content=body)]
              + [_sse(finish="length")] + [_sse(finish="length", usage=USAGE)]
              + [_sse(done=True)])
    out = _run(chunks)
    assert body in "".join(_contents(out)), _contents(out)


def test_empty_truncated_stream_still_delivered_not_refused():
    """REGRESSION GUARD for the 2026-09-10 arm (degenerate.py:441-450): an empty body with
    finish_reason=length is delivered, not refused as wedged. That fix stopped an opencode step
    failing 9 times running; this change must not undo it."""
    chunks = ([_sse(role=True)] + [_sse(content="", reasoning=f) for f in COT_FRAGMENTS]
              + [_sse(finish="length")] + [_sse(finish="length", usage=USAGE)]
              + [_sse(done=True)])
    out = _run(chunks)          # must not raise
    assert "length" in _finishes(out)
    assert _has_usage(out)


def test_short_coincidental_equality_is_not_treated_as_cot():
    """A one-word content that happens to equal a one-word side channel is a coincidence, not a
    chain-of-thought. Below the evidence threshold, leave it alone."""
    chunks = ([_sse(role=True)] + [_sse(content="", reasoning="Hmm")]
              + [_sse(content="Hmm")]
              + [_sse(finish="length")] + [_sse(finish="length", usage=USAGE)]
              + [_sse(done=True)])
    out = _run(chunks)
    assert "Hmm" in "".join(_contents(out)), _contents(out)
