"""Wedge guard: the EMPTY-BODY arm and its opt-in.

Background. degenerate.wedge_reason() hunts a *contradiction* -- a body that claims tokens it
did not produce, or repeats itself. A completely empty body is not a contradiction, so until
2026-09-02 it returned None and the void was delivered to the caller as a successful empty
completion. That is the hole traced from the pipeline's one `empty_stage` row (09-01 22:47:27,
2_qwen_coder, ttft=0, chars=0, tok_in=0, tok_out=0 -- a backend void, not model behavior).

The fix cannot be unconditional: /api/generate (FIM/autocomplete) is guarded by the same
guard_stream, and completing to nothing there is a legitimate "no suggestion". Turning that
into a 502 would break the editor. So the arm is OPT-IN per call site and defaults OFF.

These tests pin both halves: default behavior must be byte-for-byte what it was, and the
opt-in must fire on a void while still passing a tool-call turn (an assistant turn that calls
a tool legitimately has no content).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from degenerate import (  # noqa: E402
    WedgedBackendError,
    check_openai_response,
    guard_stream,
    has_tool_calls,
    sse_has_tool_calls,
    wedge_reason,
)

# Varied prose, deliberately NOT "x " * 40: that fixture trips the REPETITION arm instead of
# the token arm, so a test using it passes for the wrong reason.
LONG = ("The router resolved tier 2 and dispatched the draft stage to the local "
        "backend, then waited for the first token before opening the gate.")


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _text_chunk(t):
    return _sse({"choices": [{"delta": {"content": t}}]})


TOOL_CHUNK = _sse({"choices": [{"delta": {"tool_calls": [
    {"index": 0, "function": {"name": "git_read", "arguments": "{}"}}]}}]})


async def _agen(items):
    for i in items:
        yield i


def _drain(*items, **kw):
    """guard_stream is an ASYNC generator; drive it and return the concatenated output."""
    async def go():
        return "".join([c async for c in guard_stream(_agen(items), **kw)])
    return asyncio.run(go())


# --- default behavior must not change ---------------------------------------------------

def test_normal_text_is_not_wedged():
    assert wedge_reason(LONG, {"completion_tokens": 30}) is None


def test_empty_body_default_is_not_wedged():
    assert wedge_reason("") is None
    assert wedge_reason("", {"completion_tokens": 0}) is None


def test_token_contradiction_still_fires():
    assert wedge_reason(LONG, {"completion_tokens": 0}) is not None


def test_silence_is_not_a_contradiction():
    """No usage block means no claim to contradict."""
    assert wedge_reason(LONG, None) is None


def test_empty_stream_default_is_delivered():
    out = _drain(_text_chunk(""), endpoint="probe", model="m")
    assert "wedged" not in out.lower()


# --- the opt-in arm ------------------------------------------------------------------------

def test_empty_body_opt_in_is_wedged():
    r = wedge_reason("", {"completion_tokens": 0}, empty_is_wedged=True)
    assert r is not None and "0 chars" in r


def test_whitespace_only_opt_in_is_wedged():
    assert wedge_reason("   \n\t ", empty_is_wedged=True) is not None


def test_tool_call_turn_opt_in_is_not_wedged():
    """An assistant turn that calls a tool has no content by design."""
    assert wedge_reason("", has_tool_calls=True, empty_is_wedged=True) is None


def test_real_text_opt_in_is_not_wedged():
    assert wedge_reason(LONG, {"completion_tokens": 30}, empty_is_wedged=True) is None


# --- tool-call detection, which is what keeps the arm safe ---------------------------------

def test_has_tool_calls_message_shape():
    assert has_tool_calls({"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]}) is True


def test_has_tool_calls_delta_shape():
    assert has_tool_calls({"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]}) is True


def test_has_tool_calls_plain_content():
    assert has_tool_calls({"choices": [{"message": {"content": "hi"}}]}) is False


def test_has_tool_calls_never_raises_on_garbage():
    for junk in (None, {}, {"choices": None}, {"choices": [None]}, {"choices": "x"}, 7):
        assert has_tool_calls(junk) is False


def test_sse_has_tool_calls():
    assert sse_has_tool_calls(TOOL_CHUNK) is True
    assert sse_has_tool_calls(_text_chunk("hello")) is False
    assert sse_has_tool_calls("data: [DONE]\n\n") is False


# --- end to end through guard_stream -------------------------------------------------------

def test_guard_stream_empty_opt_in_refuses():
    """guard_stream RAISES on a wedge -- that is what becomes the 502. It does not
    yield an apology chunk, so asserting on the output text would silently pass."""
    try:
        _drain(_text_chunk(""), endpoint="probe", model="m", empty_is_wedged=True)
    except WedgedBackendError as e:
        assert "0 chars" in str(e)
    else:
        raise AssertionError("empty stream with opt-in was DELIVERED, not refused")


def test_guard_stream_tool_call_opt_in_delivers():
    """The regression that matters: the fix must not turn tool use into a 502."""
    out = _drain(TOOL_CHUNK, endpoint="probe", model="m", empty_is_wedged=True)
    assert "wedged" not in out.lower()
    assert "tool_calls" in out


def test_guard_stream_normal_opt_in_delivers():
    out = _drain(_text_chunk(LONG), endpoint="probe", model="m", empty_is_wedged=True)
    assert "wedged" not in out.lower()
    assert "router resolved" in out


def test_check_openai_response_default_delivers_empty():
    resp = {"choices": [{"message": {"content": ""}}], "usage": {"completion_tokens": 0}}
    check_openai_response(resp, endpoint="probe", model="m")  # must not raise


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for n, f in fns:
        try:
            f()
            print("  PASS  %s" % n)
        except Exception as e:
            bad += 1
            print("  FAIL  %s -- %s: %s" % (n, type(e).__name__, e))
    print("\n  %d passed, %d failed" % (len(fns) - bad, bad))
    sys.exit(1 if bad else 0)
