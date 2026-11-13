"""
reasoning_coalesce.py - fix for thinking-model empty-content responses.

Some BIFROST tiers run thinking-capable models that emit chain-of-thought in a
side-channel field instead of (or before) the visible `content` field. When the
caller passes a constrained `max_tokens`, the model spends its budget on the
side-channel and `content` arrives empty.

Side-channel field names observed in the fleet as of 2026-04-25:
  - Ollama native `/api/chat` (qwen3, gemma4, search models): `message.thinking`
  - llama.cpp llama-server (Qwen3 GGUF): `message.reasoning_content`
  - Generic OpenAI extension (some compat shims): `message.reasoning`

Without this coalescer, the Router returns HTTP 200 with `content=""` to the
caller, which clients interpret as a broken backend.

Strategy: when content is empty/whitespace, promote the first non-empty
side-channel field into content. Mark the response with
`_bifrost_reasoning_promoted: true` for observability.

Streaming responses are NOT handled here - SSE chunks split content and
side-channel across the stream and require buffering. See PHASE C.6 deferred
note in SESSION-ROUTER-V91 brief.
"""

import logging
import os
import re

log = logging.getLogger("bifrost.reasoning_coalesce")

REASONING_FIELDS = ("thinking", "reasoning", "reasoning_content")


def coalesce_reasoning_into_content(response: dict) -> dict:
    """If response content is empty but a side-channel reasoning field is
    populated, promote that field's value into content. Mutates the dict
    in place AND returns it.

    Behavior:
      non-empty content                    -> unchanged
      empty content + populated reasoning  -> reasoning copied to content,
                                              response['_bifrost_reasoning_promoted'] = True
      empty content + no reasoning         -> finish_reason "stop" -> "length" IF
                                              the provider billed reasoning tokens;
                                              see _flag_reasoning_starved
      non-empty content, mostly reasoning  -> unchanged, LOGGED only (nothing is
                                              added to the payload); see
                                              _flag_reasoning_dominant
      malformed response (no choices)      -> unchanged
    """
    if not isinstance(response, dict):
        return response

    try:
        msg = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return response

    if not isinstance(msg, dict):
        return response

    content = msg.get("content") or ""
    if isinstance(content, str) and content.strip():
        # Has real content -- but it may be reasoning LEAKED INLINE rather than into a
        # side-channel field. Same defect, different shape; see _handle_inline_think.
        return _flag_reasoning_dominant(_handle_inline_think(response, msg, content))

    for field in REASONING_FIELDS:
        fallback = msg.get(field)
        if isinstance(fallback, str) and fallback.strip():
            log.info(
                "coalesce: promoting message.%s (%d chars) into empty content",
                field, len(fallback),
            )
            msg["content"] = fallback
            response["_bifrost_reasoning_promoted"] = True
            response["_bifrost_reasoning_field"] = field
            return response

    return _flag_reasoning_starved(response)
# Duplicated from backends.ollama.strip_think_blocks rather than imported: that module imports
# THIS one, so the import would be circular. Kept byte-identical to its pattern on purpose.
_CLOSED_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN = "<think>"


def _handle_inline_think(response: dict, msg: dict, content: str) -> dict:
    """Handle reasoning that leaked into `content` inline, wrapped in <think> tags.

    Three cases, deliberately treated differently:

      closed block(s) + an answer  -> strip the blocks, keep the answer
                                      response['_bifrost_think_stripped'] = True
      closed block(s), NO answer   -> leave content ALONE (stripping gives an empty 200)
                                      response['_bifrost_reasoning_only'] = True
      UNCLOSED block               -> leave content ALONE, and rewrite finish_reason
                                      "stop" -> "length", because the generation was cut off
                                      mid-reasoning and said "stop"
                                      response['_bifrost_reasoning_truncated'] = True

    The two no-answer cases do not strip. An empty HTTP 200 is the failure this module exists
    to prevent, so trading a visible leak for an invisible blank is a downgrade.
    """
    if _THINK_OPEN not in content:
        return response

    stripped = _CLOSED_THINK_RE.sub("", content).strip()

    if _THINK_OPEN in stripped:
        # An opening tag survived the closed-block substitution, so it never closed.
        log.info("coalesce: UNCLOSED <think> in content (%d chars) -- reporting truncation",
                 len(content))
        response["_bifrost_reasoning_truncated"] = True
        try:
            choice = response["choices"][0]
        except (KeyError, IndexError, TypeError):
            return response
        if isinstance(choice, dict) and choice.get("finish_reason") == "stop":
            choice["_bifrost_original_finish_reason"] = "stop"
            choice["finish_reason"] = "length"
        return response

    if not stripped:
        # The model closed its reasoning and then said nothing. That IS a complete generation,
        # so finish_reason is left alone -- only the shape is reported.
        log.info("coalesce: content is reasoning-only (%d chars), leaving intact", len(content))
        response["_bifrost_reasoning_only"] = True
        return response

    log.info("coalesce: stripped <think> block(s), %d -> %d chars", len(content), len(stripped))
    msg["content"] = stripped
    response["_bifrost_think_stripped"] = True
    return response

# ---- untagged reasoning ---------------------------------------------------------------------
# <think> blocks and side-channel fields both ANNOUNCE themselves. A model can equally spend
# its budget reasoning in PLAIN PROSE inside content, with nothing to match on -- reproduced
# live on 2026-10-03, where 58 of 64 completion tokens were reasoning and the answer was never
# reached. There is no text pattern for that shape: a prose-pattern list ("First, I need to...",
# "Let me think") fires on any response that legitimately narrates its own working, and
# refusal-marker lists have already been recorded as missing the polite case. The provider's own
# token accounting is the only non-guessing signal available, and it is already parsed exactly
# this way in free_tier_budget.py:234-241.
_REASONING_DOMINANT_DEFAULT = 0.70


def _reasoning_counts(response: dict) -> tuple:
    """(completion_tokens, reasoning_tokens). 0 for anything absent or unparsable -- a
    provider that reports no usage block must read as "no signal", never as a ratio of 0/0."""
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    det = usage.get("completion_tokens_details") or {}
    if not isinstance(det, dict):
        return 0, 0
    try:
        return int(usage.get("completion_tokens") or 0), int(det.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _dominant_threshold() -> float:
    """BIFROST_REASONING_DOMINANT_RATIO, default 0.70. Anything outside (0, 1] is ignored
    rather than honored: a threshold above the observed distribution is INERT, and a
    threshold of 0 would flag every response that reports any reasoning at all."""
    raw = os.getenv("BIFROST_REASONING_DOMINANT_RATIO", "").strip()
    if not raw:
        return _REASONING_DOMINANT_DEFAULT
    try:
        v = float(raw)
    except ValueError:
        log.warning("coalesce: BIFROST_REASONING_DOMINANT_RATIO=%r is not a number, using %.2f",
                    raw, _REASONING_DOMINANT_DEFAULT)
        return _REASONING_DOMINANT_DEFAULT
    if not (0.0 < v <= 1.0):
        log.warning("coalesce: BIFROST_REASONING_DOMINANT_RATIO=%r out of (0,1], using %.2f",
                    raw, _REASONING_DOMINANT_DEFAULT)
        return _REASONING_DOMINANT_DEFAULT
    return v


def _flag_reasoning_dominant(response: dict) -> dict:
    """REPORT ONLY: most of what the caller paid for was reasoning, not answer.

    Content is non-empty, so there IS an answer and nothing here touches it. Where the
    reasoning prose ends and the answer begins is exactly what cannot be known without a
    marker, so this does not cut, and it does not rewrite finish_reason either -- a response
    that produced visible content is not a truncation on this evidence alone.

    Denominator is max(ct, rt), not ct: whether completion_tokens INCLUDES reasoning_tokens is
    provider-dependent, and under the non-inclusive reading a raw rt/ct can exceed 1.

    Nothing is written to the response. A _bifrost_* marker ships to the caller only
    when the Router ALTERED the response -- _bifrost_reasoning_truncated and
    _bifrost_reasoning_starved both accompany a rewritten finish_reason, and
    _bifrost_think_stripped a rewritten content. This branch rewrites neither, so it
    logs and returns. Tests assert the log record, not an absent key: asserting only
    that a key is missing passes just as well when the branch never runs at all.
    """
    if not isinstance(response, dict):
        return response
    ct, rt = _reasoning_counts(response)
    if rt <= 0:
        return response
    denom = max(ct, rt)
    if denom <= 0:
        return response
    ratio = rt / float(denom)
    if ratio < _dominant_threshold():
        return response
    # LOG ONLY, nothing added to the payload (John 2026-10-04). The ratio is an
    # operator signal, not something a caller asked for, and this branch changes
    # NOTHING about the response -- so by the rule below there is nothing to declare.
    log.info("coalesce: reasoning-dominant response -- %d of %d completion tokens "
             "(%.0f%%) were reasoning; content and finish_reason left intact, "
             "nothing added to the payload", rt, denom, ratio * 100.0)
    return response


def _flag_reasoning_starved(response: dict) -> dict:
    """An empty 200 that the provider called a COMPLETION, after billing reasoning tokens.

    Reached only when content is empty AND no side-channel field held anything to promote. If
    the provider billed reasoning tokens, the budget went to reasoning and the answer was never
    reached -- so finish_reason "stop" is false, and is corrected to "length" using the same
    preserved-original convention _handle_inline_think already applies to an unclosed <think>
    block.

    free_tier_budget.py:190-194 already COUNTS this shape as `starved` from a live observation
    (laguna-s-2.1: 179 reasoning tokens, 0 chars, finish_reason "stop"). It was counted
    internally and never reported to the caller, who saw a successful empty answer.
    """
    ct, rt = _reasoning_counts(response)
    if rt <= 0:
        return response
    try:
        choice = response["choices"][0]
    except (KeyError, IndexError, TypeError):
        return response
    if not isinstance(choice, dict):
        return response
    log.info("coalesce: empty content after %d reasoning / %d completion tokens -- starved",
             rt, ct)
    response["_bifrost_reasoning_starved"] = True
    if choice.get("finish_reason") == "stop":
        choice["_bifrost_original_finish_reason"] = "stop"
        choice["finish_reason"] = "length"
    return response
