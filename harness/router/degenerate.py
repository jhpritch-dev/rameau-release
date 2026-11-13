"""Detect a WEDGED backend: HTTP 200, plausible shape, and no actual answer.

WHY THIS EXISTS AT THE BACKEND LAYER
------------------------------------
The dominant failure on this fleet is a process that RUNS, REPORTS SUCCESS and DOES
NOTHING. A wedged Ollama instance is the purest form: HTTP 200, finish_reason "stop",
NULL token counts, and a body of nothing but reserved vocabulary tokens -- for EVERY
request until the model is unloaded. Measured 2026-08-07 (a tier-2 leg returned
"<unused24>" x31) and again 2026-08-13, when it silently poisoned KB /query answers for
an unknown length of time before anyone noticed.

Equivalent logic already existed in TWO places and guarded NEITHER path that actually
failed:
  * subtask_graph.py -- autopilot legs only, so ordinary Router traffic was unguarded;
  * the KB service   -- its own /query, added in KB 1.19 after the fact.

Hence this module: ONE implementation at the layer every request passes through.
subtask_graph now imports from here instead of keeping a second copy. The KB is a
separate service and keeps its own; that duplication is known, not overlooked.

TWO INDEPENDENT SIGNALS
-----------------------
1. RESERVED-TOKEN OUTPUT. Lifted verbatim from subtask_graph, so autopilot behavior is
   unchanged. Deliberately conservative: real code legitimately repeats short lines, and
   discarding a usable answer is worse than the doom loop it prevents.

2. THE TOKEN-COUNT CONTRADICTION. Available only here, where the raw backend response is
   visible -- subtask_graph sees text alone. A backend returning a substantial body while
   reporting ZERO generated tokens is contradicting itself, and that self-contradiction is
   the wedged-instance fingerprint. It catches a wedge whose filler is not reserved tokens.

   This fires only when usage is PRESENT and claims zero. A backend that omits usage is
   making no claim, so it cannot be caught contradicting one. That asymmetry is the point:
   never invent a fault from missing data.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("bifrost.degenerate")

# Reserved / special model-vocabulary tokens. A leaf that emits these is not answering.
RESERVED_TOKEN = re.compile(r"<unused\d+>|<\|[^|>\s]{1,40}\|>|<pad>|<mask>|<extra_id_\d+>")

# Below this length, a zero token count is not treated as a contradiction. Short bodies
# are where odd-but-honest accounting actually happens; the wedge always produced far more.
MIN_BODY_FOR_TOKEN_CONTRADICTION = 40

# "raise" (default) refuses the answer so the caller sees a failure instead of garbage.
# "log" detects and records only, changing no behavior -- an off-switch that keeps the
# telemetry. Anything else is treated as "raise" rather than silently disabling the check.
WEDGE_ACTION = os.getenv("BIFROST_WEDGE_ACTION", "raise").strip().lower()


class WedgedBackendError(RuntimeError):
    """A backend returned 200 but no usable answer.

    Subclasses RuntimeError so existing `except Exception` fallback paths treat it as an
    ordinary backend failure and escalate/fall back, rather than meeting a new error type.
    """


def degenerate_ratio(text: str) -> float:
    """Fraction of the output made up of reserved model-vocabulary tokens."""
    if not isinstance(text, str) or not text.strip():
        return 0.0
    s = text.strip()
    return len("".join(RESERVED_TOKEN.findall(s))) / len(s)


def looks_degenerate(text: str) -> bool:
    """True when output is model noise rather than an attempt at the task.

    Two shapes:
      - reserved tokens dominate -- measured 2026-08-07, tier 2 returned "<unused24>" x31;
      - a couple of fragments repeated to fill the response.

    Deliberately conservative. The repetition arm demands BOTH a long output AND almost no
    distinct fragments, because a false positive throws away a usable attempt.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    s = text.strip()
    if degenerate_ratio(s) >= 0.5:
        return True
    parts = s.split()
    return len(parts) >= 20 and len(set(parts)) <= 2


def _completion_tokens(usage):
    """Return the backend's claimed completion-token count, or None if it made no claim.

    None and 0 mean different things here and must not be conflated: None is silence,
    0 is an assertion. Only an assertion can contradict the body.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("completion_tokens", "eval_count"):
        if key in usage:
            v = usage.get(key)
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def wedge_reason(text, usage=None, has_tool_calls=False, empty_is_wedged=False,
                 finish_reason=None):
    """Return a human-readable reason if this response is wedged, else None.

    `empty_is_wedged` is OPT-IN and defaults False, so no existing caller changes behavior.
    See the EMPTY-BODY arm below for why it cannot simply be always-on.
    """
    body = text if isinstance(text, str) else ""

    # EMPTY-BODY arm (2026-09-02). The two arms below both need a body to work with:
    # looks_degenerate("") is False, and the token-contradiction arm requires >= 40 chars
    # because it hunts a CONTRADICTION (text present, 0 tokens claimed). So the emptiest
    # possible wedge -- 0 chars, 0 tokens -- passed every check, which is the one case this
    # module's own docstring ("no actual answer") most obviously describes.
    #
    # Measured: MoA pipeline stage 1 on 2_qwen_coder, 2026-09-01 22:47:27 -- 616 ms, 0 chars,
    # tok_in 0, tok_out 0. The backend returned without even counting the prompt. The pipeline
    # only noticed downstream, by accident, via its own "empty_stage" break. On INTERACTIVE or
    # TWO_PASS the same void reaches the caller as a SUCCESSFUL empty completion.
    #
    # Why opt-in rather than always-on -- two legitimate empties exist:
    #   1. A tool-call response carries no content by design. Hence has_tool_calls.
    #   2. FIM / autocomplete (/api/generate) may legitimately complete to nothing, and it
    #      IS wrapped by guard_stream (backends/ollama.py:308). Refusing that would turn a
    #      normal "no suggestion" into a 502 in the editor.
    # So the CALL SITE decides, because the call site is what knows which shape it is.
    if empty_is_wedged and not body.strip() and not has_tool_calls:
        # TRUNCATED, NOT WEDGED (2026-10-06) -- the NON-STREAMING half of the exemption
        # guard_stream has carried since 2026-09-10. There, an empty body whose backend
        # reported finish_reason "length" is delivered rather than refused, because it hit
        # its window or num_predict before emitting content -- typically with every token
        # spent on reasoning. Here the same response raised, so ONE instance was refused at
        # stream=False and delivered at stream=True: #98 in miniature, which is the defect
        # this module exists to prevent.
        #
        # Measured: four Lane C runs lost on 2026-10-06 to
        #   .../api/chat qwen3.5-4b-32k: NO content at all (0 chars) and claimed 53
        #   generated tokens
        # with the backend healthy on either side of each failure -- 10 of 10 Router arms
        # at max_tokens=4096 and 9 of 9 direct arms returned content. Reproduced directly:
        # at num_predict=512 the model put all 512 tokens in `thinking` and returned
        # content="" with done_reason="length".
        #
        # The coalescer cannot catch this one. backends/ollama.py:742 preserves
        # message.thinking so it CAN be promoted, and when thinking is non-empty it is --
        # no wedge. The failing shape is empty content AND empty thinking with tokens still
        # billed; _flag_reasoning_starved then reads reasoning_tokens, which Ollama never
        # sends, so it returns untouched. finish_reason is the only signal left, and
        # ollama.py:765 already puts it on the shaped response.
        #
        # Scope is the empty body ONLY. Per the streaming comment, "a NON-empty degenerate
        # body is not excused by 'length'" -- the arms below are unchanged and still refuse
        # reserved-token, repetition and token-contradiction output whatever finish_reason
        # says. A caller receives the backend's own "length" and can retry on more budget.
        if finish_reason == "length":
            return None
        n = _completion_tokens(usage)
        claim = "and claimed %d generated tokens" % n if n else "with no token claim"
        if n == 0:
            claim = "and claimed 0 generated tokens"
        return ("backend returned NO content at all (0 chars) %s -- "
                "nothing was generated (wedged instance)" % claim)

    # Name WHICH arm fired. Reporting "reserved-token / repetition (0% reserved)" for a
    # repetition trip is accurate but reads as a contradiction at 3am.
    if looks_degenerate(body):
        ratio = degenerate_ratio(body)
        if ratio >= 0.5:
            return ("reserved-token output (%.0f%% of %d chars are reserved vocabulary)"
                    % (100 * ratio, len(body.strip())))
        distinct = len(set(body.split()))
        return ("repetition output (%d chars, only %d distinct fragment(s))"
                % (len(body.strip()), distinct))

    n = _completion_tokens(usage)
    if n == 0 and len(body.strip()) >= MIN_BODY_FOR_TOKEN_CONTRADICTION:
        return ("backend returned %d chars while reporting 0 generated tokens "
                "(self-contradictory -- wedged instance)" % len(body.strip()))

    return None


def content_of(resp) -> str:
    """Best-effort extraction of the assistant text from an OpenAI-shaped response.

    Never raises: a shape this does not understand yields "", which trips nothing. A
    detector that could itself throw on the hot path would be worse than the bug it hunts.
    """
    try:
        choices = (resp or {}).get("choices") or []
        if not choices:
            return ""
        c0 = choices[0] or {}
        msg = c0.get("message") or {}
        return msg.get("content") or c0.get("text") or ""
    except Exception:  # noqa: BLE001
        return ""


def finish_reason_of(resp):
    """finish_reason off an OpenAI-shaped response, or None.

    Never raises, for the same reason content_of and has_tool_calls do not: a detector
    that can throw on the hot path is worse than the bug it hunts.
    """
    try:
        c0 = ((resp or {}).get("choices") or [{}])[0] or {}
        v = c0.get("finish_reason")
        return v if isinstance(v, str) else None
    except Exception:  # noqa: BLE001
        return None


def check_openai_response(resp, endpoint=None, model=None, empty_is_wedged=False):
    """check_response() for an already-shaped OpenAI response dict.

    Call this on the response the CALLER will receive -- i.e. after any coalescing -- so
    the check evaluates the real answer rather than an intermediate form.
    """
    # isinstance, not `(resp or {}).get` -- a non-dict resp (a bare string from some future
    # backend shape) made that form raise AttributeError on the hot path. Caught by the
    # malformed-shape cell. A detector that can throw is worse than the bug it hunts.
    usage = resp.get("usage") if isinstance(resp, dict) else None
    return check_response(content_of(resp), usage, endpoint=endpoint, model=model,
                          has_tool_calls=has_tool_calls(resp),
                          empty_is_wedged=empty_is_wedged,
                          finish_reason=finish_reason_of(resp))


def has_tool_calls(resp) -> bool:
    """True when an OpenAI-shaped response carries tool calls.

    A tool-call response has NO content by design, so the empty-body arm must not fire on it.
    Never raises, for the same reason content_of does not.
    """
    try:
        c0 = ((resp or {}).get("choices") or [{}])[0] or {}
        msg = c0.get("message") or c0.get("delta") or {}
        return bool(msg.get("tool_calls") or msg.get("function_call"))
    except Exception:  # noqa: BLE001
        return False


def check_response(text, usage=None, endpoint=None, model=None, can_refuse=True,
                   has_tool_calls=False, empty_is_wedged=False, finish_reason=None):
    """Assert a backend response is a real answer.

    Raises WedgedBackendError when wedged and BIFROST_WEDGE_ACTION is "raise" (default).
    Always logs at ERROR and always increments the metric, so setting the action to "log"
    silences the refusal but never the evidence.

    `can_refuse=False` says the answer has ALREADY been delivered and cannot be recalled --
    the streaming end-of-stream case. It logs and counts exactly as usual and returns the
    reason instead of raising, so the evidence still exists where the refusal cannot.

    Returns the reason string when wedged (action="log", or can_refuse=False), else None.
    """
    reason = wedge_reason(text, usage, has_tool_calls=has_tool_calls,
                          empty_is_wedged=empty_is_wedged, finish_reason=finish_reason)
    if reason is None:
        return None

    where = "%s model=%s" % (endpoint or "?", model or "?")
    log.error("WEDGED BACKEND at %s: %s. First 120 chars: %r",
              where, reason, (text or "")[:120])

    try:
        from metrics import BACKEND_WEDGED_TOTAL
        BACKEND_WEDGED_TOTAL.labels(endpoint=str(endpoint or "unknown")).inc()
    except Exception:  # noqa: BLE001 -- telemetry must never break dispatch
        pass

    if WEDGE_ACTION == "log" or not can_refuse:
        return reason
    raise WedgedBackendError("%s: %s" % (where, reason))


# ---------------------------------------------------------------------------
# STREAMING (#98)
# ---------------------------------------------------------------------------
#
# Everything above judges a COMPLETE response, and #87 wired it into the non-streaming paths
# only. So the same wedged instance was REFUSED when a caller passed stream=False and
# delivered verbatim when it passed stream=True. Nothing about the backend differed between
# those two outcomes; only the flag did.
#
# Streaming cannot just reuse the check, because bytes already sent cannot be recalled, and
# buffering the whole answer to make the check possible would destroy the point of
# streaming. So the guard withholds output only while everything seen SO FAR is still
# consistent with a wedge, and forwards it the moment it is not:
#
#   healthy answer -- ordinary prose rules out both wedge shapes within about three distinct
#                     words, so the hold costs a few tokens of latency and nothing else;
#   wedged answer  -- never rules them out, and is refused with ZERO bytes delivered.
#
# WHAT THIS STILL CANNOT DO. A stream that opens as real prose and degenerates later is
# already past the hold. That case is logged and counted at end-of-stream but NOT refused,
# because the answer is gone. Saying otherwise would be the "reports success, does nothing"
# failure this module exists to catch, so check_response(can_refuse=False) states it in the
# call rather than in a comment.

# Safety valve. Nothing observed holds this long -- both wedge shapes trip their thresholds
# far sooner -- but a stream of pure whitespace would satisfy "still suspect" forever, and an
# unbounded hold is indistinguishable from a hang.
STREAM_HOLD_MAX_CHARS = 2000

# Partial evidence needs a floor, and this is the one place streaming must be MORE cautious
# than the complete-response check rather than less. looks_degenerate will happily condemn a
# ten-character body, which is correct for a finished answer and wrong for a prefix: a real
# answer that opens by quoting a special token -- "<|im_start|> marks the start of a turn"
# -- is 100% reserved vocabulary for exactly one chunk, and the non-streaming check never
# sees that moment. Below this many characters the guard keeps HOLDING rather than refusing.
# Nothing is lost by waiting: if the stream ends down here, the end-of-stream path judges the
# whole body with the ordinary check, refusal included.
STREAM_MIN_EVIDENCE_CHARS = 40

# A content delta byte-identical to the WHOLE accumulated side channel is the thinking re-emitted
# as the answer, not an answer. Measured 2026-10-05 on 3-lite-free at finish_reason=length:
# 208/208 chars at max_tokens=60 and 586/586 at 160, content reading "Here's a thinking process:
# 1. **Analyze User Input:** ...". Below this many chars, treat equality as coincidence -- a
# one-word content matching a one-word `reasoning` says nothing about either.
COT_AS_CONTENT_MIN_CHARS = 40


def stream_still_suspect(text) -> bool:
    """True while everything generated SO FAR is still consistent with a wedge.

    This is the partial-evidence complement of looks_degenerate, not a copy of it.
    looks_degenerate answers "is this finished body degenerate", which needs the whole
    answer; this answers "could this still turn out to be", which is all a partial stream can
    support. BOTH arms are represented -- reserved-vocabulary dominance and too-few distinct
    fragments -- so a stream is released only once it has ruled out both.
    """
    s = (text or "").strip()
    if not s:
        return True
    if degenerate_ratio(s) >= 0.5:
        return True
    return len(set(s.split())) <= 2


def sse_delta_text(chunk) -> str:
    """Assistant text carried by one SSE chunk; "" for anything not understood.

    Reads choices[0].delta.content AND choices[0].text: /api/generate (FIM, autocomplete)
    streams the latter, and a delta-only reader would leave that path unguarded while the
    suite stayed green -- the same shape of gap as #98 itself.
    """
    try:
        if not isinstance(chunk, (str, bytes)):
            return ""
        line = (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk).strip()
        if not line.startswith("data:"):
            return ""
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return ""
        obj = json.loads(body)
        choices = obj.get("choices") or []
        if not choices:
            return ""
        c0 = choices[0] or {}
        return (c0.get("delta") or {}).get("content") or c0.get("text") or ""
    except Exception:  # noqa: BLE001 -- a detector that throws on the hot path is worse
        return ""


def sse_delta_reasoning(chunk) -> str:
    """Side-channel text carried by one SSE chunk; "" for anything not understood.

    Reads delta.reasoning / reasoning_content / thinking -- the three names the thinking models
    in this fleet actually use. sse_delta_text deliberately does NOT read these: the wedge arms
    judge the ANSWER, and folding reasoning into that judgment would make a model that answered
    nothing look answered.
    """
    try:
        if not isinstance(chunk, (str, bytes)):
            return ""
        line = (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk).strip()
        if not line.startswith("data:"):
            return ""
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return ""
        c0 = (json.loads(body).get("choices") or [{}])[0] or {}
        d = c0.get("delta") or {}
        for k in ("reasoning", "reasoning_content", "thinking"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
        return ""
    except Exception:  # noqa: BLE001 -- a detector that throws on the hot path is worse
        return ""


def content_is_cot(text, side) -> bool:
    """True when everything delivered as CONTENT is exactly the accumulated side channel.

    Byte equality is the whole test, and it is strict on purpose: a provider that truncates
    mid-thought re-emits the thinking verbatim, so anything short of equality is a real -- if
    cut off -- answer and must be left alone.
    """
    t = (text or "").strip()
    s = (side or "").strip()
    return bool(t) and t == s and len(s) >= COT_AS_CONTENT_MIN_CHARS


def sse_with_empty_content(chunk):
    """The same chunk with delta.content (and choices[0].text) emptied, everything else kept.

    Returns the chunk UNCHANGED when it carries no content or cannot be parsed. The finish and
    usage chunks must survive untouched -- the entire point of emptying content is that
    finish_reason and usage still reach the client.
    """
    try:
        if not isinstance(chunk, (str, bytes)):
            return chunk
        raw = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        line = raw.strip()
        if not line.startswith("data:"):
            return chunk
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return chunk
        obj = json.loads(body)
        choices = obj.get("choices") or []
        if not choices:
            return chunk
        c0 = choices[0] or {}
        touched = False
        d = c0.get("delta")
        if isinstance(d, dict) and d.get("content"):
            d["content"] = ""
            touched = True
        if c0.get("text"):
            c0["text"] = ""
            touched = True
        if not touched:
            return chunk
        return "data: %s\n\n" % json.dumps(obj)
    except Exception:  # noqa: BLE001
        return chunk


def sse_has_tool_calls(chunk) -> bool:
    """True when one SSE chunk carries a tool call (delta.tool_calls / function_call).

    Streamed tool calls arrive with EMPTY delta.content, so without this the empty-body arm
    would refuse every streaming tool call -- a fix that breaks the thing it was protecting.
    """
    try:
        if not isinstance(chunk, (str, bytes)):
            return False
        line = (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk).strip()
        if not line.startswith("data:"):
            return False
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return False
        c0 = (json.loads(body).get("choices") or [{}])[0] or {}
        d = c0.get("delta") or c0.get("message") or {}
        return bool(d.get("tool_calls") or d.get("function_call"))
    except Exception:  # noqa: BLE001
        return False


def sse_usage(chunk):
    """Usage block from one SSE chunk, or None. It rides the FINAL chunk, which is why the
    token-count contradiction is only decidable at end-of-stream."""
    try:
        if not isinstance(chunk, (str, bytes)):
            return None
        line = (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk).strip()
        if not line.startswith("data:"):
            return None
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return None
        u = json.loads(body).get("usage")
        return u if isinstance(u, dict) else None
    except Exception:  # noqa: BLE001
        return None


def sse_finish_reason(chunk):
    """choices[0].finish_reason from one SSE chunk, or None. Never raises.

    Read so the end-of-stream verdict can tell a VOID from a TRUNCATION: a backend that
    ran out of window reports finish_reason "length", which is an answer about the request,
    not a wedged instance."""
    try:
        if not isinstance(chunk, (str, bytes)):
            return None
        line = (chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk).strip()
        if not line.startswith("data:"):
            return None
        body = line[5:].strip()
        if not body or body == "[DONE]":
            return None
        c0 = (json.loads(body).get("choices") or [{}])[0] or {}
        fr = c0.get("finish_reason")
        return fr if isinstance(fr, str) else None
    except Exception:  # noqa: BLE001
        return None


async def guard_stream(chunks, endpoint=None, model=None, empty_is_wedged=False):
    """Yield SSE chunks, refusing a wedged stream before any of it reaches the client.

    Raises WedgedBackendError -- same type, same action switch, same metric as the
    non-streaming check -- while still holding every chunk, so a refusal means the consumer
    received nothing at all rather than garbage followed by an apology.
    """
    held = []
    text = ""
    side = ""
    usage = None
    released = False
    saw_tools = False
    finish = None

    async for chunk in chunks:
        text += sse_delta_text(chunk)
        side += sse_delta_reasoning(chunk)
        usage = sse_usage(chunk) or usage
        finish = sse_finish_reason(chunk) or finish
        # Sticky: one tool-call chunk anywhere in the stream means this response is a tool
        # call, even though its content is empty.
        saw_tools = saw_tools or sse_has_tool_calls(chunk)

        if released:
            yield chunk
            continue

        held.append(chunk)

        # HOLD a content delta that is exactly the accumulated thinking: only finish_reason
        # can say whether that is an answer or a truncated thought, and it arrives in a LATER
        # chunk (measured: content at chunk 60, finish=length at 61). STREAM_HOLD_MAX_CHARS
        # still bounds it, so this cannot hold a stream open indefinitely.
        if content_is_cot(text, side) and len(text) < STREAM_HOLD_MAX_CHARS:
            pass
        elif not stream_still_suspect(text):
            released = True
        else:
            enough = len(text.strip()) >= STREAM_MIN_EVIDENCE_CHARS
            if enough and wedge_reason(text) is not None:
                # The one point mid-stream where a refusal is possible: nothing is delivered
                # yet. check_response raises unless BIFROST_WEDGE_ACTION=log, in which case
                # it returns and we fall through and deliver -- so the off-switch restores
                # the old behavior exactly, and logs and counts either way.
                check_response(text, usage, endpoint=endpoint, model=model,
                               has_tool_calls=saw_tools,
                               empty_is_wedged=empty_is_wedged)
                released = True
            elif len(text) >= STREAM_HOLD_MAX_CHARS:
                log.debug("stream wedge guard: releasing after %d held chars without a "
                          "verdict at %s", len(text), endpoint or "?")
                released = True

        if released:
            for c in held:
                yield c
            held = []

    if not released:
        # TRUNCATED, NOT WEDGED (2026-09-10). An EMPTY body whose backend reported
        # finish_reason "length" hit its window/num_predict limit before emitting any content
        # (e.g. all tokens went to reasoning or to a tool call the backend never finished).
        # Measured: opencode -> bifrost-t2-gemma4, prompt 12,611 tok + 3,773 generated = the
        # 16,384 window (Forge: truncated = 1). The empty-body arm refused it here AFTER the
        # 200 was committed, so the client got a broken stream with no finish_reason and no
        # usage; the step failed 9 times running. Deliver instead: the held chunks carry the
        # backend's own finish_reason "length" and usage. A NON-empty degenerate body is not
        # excused by "length" -- it still takes the ordinary check below.
        if finish == "length" and not text.strip():
            log.warning("TRUNCATED STREAM at %s model=%s: finish_reason=length with no "
                        "content (usage=%s) -- delivered, not refused as wedged",
                        endpoint or "?", model or "?", usage)
            for c in held:
                yield c
            return
        # TRUNCATED MID-THINKING (option (b), John 2026-10-05). finish_reason "length", and the
        # only content is the accumulated thinking re-emitted verbatim. The thinking is not an
        # answer and there is no answer to preserve -- the budget was spent before the model
        # reached one. Deliver EMPTY content with finish_reason and usage intact so the client
        # can see it truncated and retry on a larger budget. Contrast the arm above: that one
        # arrives empty, this one has to BE emptied.
        if finish == "length" and content_is_cot(text, side):
            log.warning("TRUNCATED MID-THINKING at %s model=%s: %d chars of content were the "
                        "chain-of-thought verbatim (usage=%s) -- delivered with content emptied",
                        endpoint or "?", model or "?", len(text.strip()), usage)
            for c in held:
                yield sse_with_empty_content(c)
            return
        # The stream ended while still held, so `text` is a COMPLETE body and gets exactly
        # the check the non-streaming path would give it -- refusal included, with nothing
        # delivered. Without this, a short all-reserved answer would be refused at
        # stream=False and passed at stream=True, which is #98 in miniature.
        check_response(text, usage, endpoint=endpoint, model=model,
                               has_tool_calls=saw_tools,
                               empty_is_wedged=empty_is_wedged)
        for c in held:
            yield c
        return

    # Past the hold: everything is already delivered, so this can only RECORD. can_refuse
    # says that in the call rather than leaving a reader to infer it from context.
    if finish == "length" and content_is_cot(text, side):
        # Says so out loud rather than failing quietly: a chain-of-thought longer than the hold
        # bound is released before finish_reason is known, so option (b) cannot be applied to it.
        log.warning("TRUNCATED MID-THINKING at %s model=%s: the chain-of-thought was already "
                    "released (%d chars, hold bound %d) and cannot be recalled",
                    endpoint or "?", model or "?", len(text.strip()), STREAM_HOLD_MAX_CHARS)
    check_response(text, usage, endpoint=endpoint, model=model, can_refuse=False,
                   has_tool_calls=saw_tools, empty_is_wedged=empty_is_wedged)
