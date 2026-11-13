"""
BIFROST Router — Two-Pass Strategy
====================================
Streams local draft to the user, then runs a confidence gate:
  - HIGH confidence → deliver draft, skip cloud (local-first wins)
  - LOW confidence  → stream cloud review after separator

Provider selection by band (when review triggers):
  COMPLEX   → Gemini (deep context)  
  FRONTIER  → Claude (compliance, frontier reasoning)

Floor: TWO_PASS only activates on COMPLEX+ bands. MODERATE stays
fully local in INTERACTIVE mode — the 7B handles it.
"""

import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator

from config import (
    ComplexityBand,
    Tier,
    settings,
)
from review_prompts import build_review_prompt, get_review_provider, detect_task_type
from backends.ollama import ollama_chat_completion
from backends.openai_compat import openai_compat_chat_completion
from backends.anthropic import anthropic_chat_completion

log = logging.getLogger("bifrost.two_pass")

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

PASS_SEPARATOR = "\n\n---\n\n"


def make_sse_chunk(content: str, finish_reason: str | None = None) -> str:
    """Build an OpenAI-format SSE data line."""
    delta = {"content": content} if content else {}
    chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}\n\n"


def extract_content_from_sse(line: str) -> str | None:
    """Pull text content from an SSE data line. Returns None on [DONE] or non-content."""
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        return None
    try:
        data = json.loads(data_str)
        return data.get("choices", [{}])[0].get("delta", {}).get("content")
    except (json.JSONDecodeError, IndexError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Confidence gate — decides whether to escalate to cloud
# ---------------------------------------------------------------------------

# Patterns that suggest the model is uncertain or hedging
HEDGING_PATTERNS = re.compile(
    r"\b(i'?m not sure|i think maybe|this might|not certain|"
    r"i don'?t know|possibly|i believe this could|"
    r"this is a rough|untested|placeholder|TODO|FIXME|hack)\b",
    re.IGNORECASE,
)

# Unclosed code blocks suggest truncation
UNCLOSED_BLOCK_RE = re.compile(r"```[\w]*\n(?!.*```)", re.DOTALL)


def confidence_gate(
    draft_text: str,
    prompt_text: str,
    band: ComplexityBand,
) -> tuple[bool, str]:
    """
    Evaluate whether the local draft is good enough to deliver without cloud review.

    Returns:
        (should_escalate: bool, reason: str)

    Philosophy: local-first. Cloud is the exception, not the default.
    The gate must find a reason TO escalate, not a reason to skip.
    """
    if not settings.confidence_gate_enabled:
        return True, "confidence_gate_disabled"

    draft_tokens = len(draft_text.split())
    prompt_tokens = max(len(prompt_text.split()), 1)

    reasons = []

    # 1. Empty or trivially short response
    if draft_tokens < settings.confidence_min_tokens:
        reasons.append(f"too_short({draft_tokens}<{settings.confidence_min_tokens})")

    # 2. Response ratio — suspiciously short for the prompt complexity
    ratio = draft_tokens / prompt_tokens
    if ratio < settings.confidence_min_response_ratio:
        reasons.append(f"low_ratio({ratio:.2f}<{settings.confidence_min_response_ratio})")

    # 3. Truncation — unclosed code blocks suggest the model ran out of context
    if settings.confidence_truncation_penalty and UNCLOSED_BLOCK_RE.search(draft_text):
        reasons.append("truncated_code_block")

    # 4. Hedging language — model expressing uncertainty
    if settings.confidence_hedging_penalty:
        hedges = HEDGING_PATTERNS.findall(draft_text)
        if len(hedges) >= 2:
            reasons.append(f"hedging({len(hedges)})")

    # 5. FRONTIER always escalates — by definition these need cloud eyes
    if band == ComplexityBand.FRONTIER:
        reasons.append("frontier_band")

    should_escalate = len(reasons) > 0
    reason_str = ", ".join(reasons) if reasons else "confident"

    return should_escalate, reason_str


# ---------------------------------------------------------------------------
# Two-pass streaming generator
# ---------------------------------------------------------------------------

async def two_pass_stream(
    messages: list[dict],
    band: ComplexityBand,
    local_tier: Tier,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """
    Generator that streams both passes to the user:
      1. Local draft streams live (user sees the 7B working)
      2. Separator
      3. Cloud review streams (user sees the improvement)

    Yields SSE-format strings.

    Returns metadata dict via .metadata attribute after completion
    (set on the generator object by the caller wrapper).
    """
    draft_tokens = []
    draft_skipped = False
    t_start = time.time()

    review_provider = get_review_provider(band)
    provider_labels = {"groq": "Groq ⚡", "gemini": "Gemini 🔬", "claude": "Claude 🎯"}
    provider_label = provider_labels.get(review_provider, review_provider)

    # --- Pass 1: Stream local draft ---
    t_draft_start = time.time()
    draft_first_token = None

    # Emit pass 1 header
    yield make_sse_chunk(f"📝 **Local Draft** ({local_tier.value}):\n\n")

    try:
        draft_stream = await asyncio.wait_for(
            ollama_chat_completion(
                messages=messages,
                tier=local_tier,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens or 4096,
            ),
            timeout=settings.two_pass_local_timeout,
        )

        async for line in draft_stream:
            content = extract_content_from_sse(line)
            if content:
                if draft_first_token is None:
                    draft_first_token = time.time()
                draft_tokens.append(content)
                yield make_sse_chunk(content)

    except asyncio.TimeoutError:
        draft_skipped = True
        log.warning(f"DRAFT: timed out after {settings.two_pass_local_timeout}s")
        yield make_sse_chunk(f"\n\n⏱️ *Draft timed out after {settings.two_pass_local_timeout}s*\n")

    except Exception as e:
        draft_skipped = True
        log.warning(f"DRAFT: failed ({type(e).__name__}: {e})")
        yield make_sse_chunk(f"\n\n⚠️ *Draft failed: {type(e).__name__}*\n")

    draft_text = "".join(draft_tokens)
    draft_ms = (time.time() - t_draft_start) * 1000
    draft_ttft = ((draft_first_token - t_draft_start) * 1000) if draft_first_token else 0

    log.info(
        f"DRAFT: tier={local_tier.value} "
        f"tokens=~{len(draft_tokens)} ttft={draft_ttft:.0f}ms "
        f"total={draft_ms:.0f}ms skipped={draft_skipped}"
    )

    # --- Confidence gate: should we escalate to cloud? ---
    if not draft_skipped and draft_text.strip():
        user_prompt = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        should_escalate, gate_reason = confidence_gate(draft_text, user_prompt, band)

        if not should_escalate:
            log.info(
                f"GATE: PASS — skipping cloud review (reason={gate_reason}). "
                f"Local draft delivered as final."
            )
            yield make_sse_chunk(
                f"\n\n✅ *Local draft accepted ({gate_reason})*\n"
            )
            total_ms = (time.time() - t_start) * 1000
            log.info(
                f"COMPLETE: strategy=TWO_PASS band={band.value} "
                f"draft_tier={local_tier.value} review=SKIPPED "
                f"gate={gate_reason} total={total_ms:.0f}ms"
            )
            yield make_sse_chunk("", finish_reason="stop")
            yield "data: [DONE]\n\n"
            return

        log.info(f"GATE: FAIL — escalating to {review_provider} (reason={gate_reason})")
    else:
        gate_reason = "draft_failed" if draft_skipped else "empty_draft"
        log.info(f"GATE: FAIL — {gate_reason}, escalating to {review_provider}")

    # --- Separator ---
    yield make_sse_chunk(PASS_SEPARATOR)
    yield make_sse_chunk(f"🔍 **Cloud Review** ({provider_label}):\n\n")

    # --- Pass 2: Stream cloud review ---
    t_review_start = time.time()
    review_first_token = None

    try:
        if draft_skipped or not draft_text.strip():
            # No usable draft — send original prompt directly
            review_messages = messages
            log.info(f"REVIEW: no draft, sending original to {review_provider}")
        else:
            task_type = detect_task_type(
                next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            )
            review_messages = build_review_prompt(
                original_messages=messages,
                draft_response=draft_text,
                provider=review_provider,
                task_type=task_type,
            )
            log.info(f"REVIEW: sending to {review_provider} (type={task_type})")

        # Dispatch to the right cloud backend
        if review_provider == "claude":
            review_stream = await anthropic_chat_completion(
                messages=review_messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens or 4096,
            )
        else:
            review_stream = await openai_compat_chat_completion(
                messages=review_messages,
                provider=review_provider,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens or 4096,
            )

        async for line in review_stream:
            content = extract_content_from_sse(line)
            if content:
                if review_first_token is None:
                    review_first_token = time.time()
                yield make_sse_chunk(content)

    except Exception as e:
        log.error(f"REVIEW: {review_provider} failed ({type(e).__name__}: {e})")
        yield make_sse_chunk(f"\n\n⚠️ *Review failed: {type(e).__name__}. Draft above is unreviewed.*\n")

    review_ms = (time.time() - t_review_start) * 1000
    review_ttft = ((review_first_token - t_review_start) * 1000) if review_first_token else 0
    total_ms = (time.time() - t_start) * 1000

    log.info(
        f"REVIEW: provider={review_provider} "
        f"ttft={review_ttft:.0f}ms total={review_ms:.0f}ms"
    )
    log.info(
        f"COMPLETE: strategy=TWO_PASS band={band.value} "
        f"draft_tier={local_tier.value} review={review_provider} "
        f"draft={draft_ms:.0f}ms review={review_ms:.0f}ms total={total_ms:.0f}ms"
    )

    # Final SSE terminator
    yield make_sse_chunk("", finish_reason="stop")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------

def select_local_tier(band: ComplexityBand) -> Tier:
    """
    Pick best available local tier for drafting.
    WORKSHOP mode: always 1a-coder.
    JARVIS mode: match to band.
    """
    from config import OperatingMode

    mode = settings.current_mode

    if mode in (OperatingMode.WORKSHOP, OperatingMode.WORKSHOP_OFFLINE):
        return Tier.T1A_CODER

    # JARVIS — tier matched to complexity
    return {
        ComplexityBand.TRIVIAL: Tier.T1A_CODER,
        ComplexityBand.MODERATE: Tier.T1A_CODER,    # 7B drafts, cloud polishes
        ComplexityBand.COMPLEX: Tier.T1B,            # 14B drafts (Forge)
        ComplexityBand.FRONTIER: Tier.T2_5,          # 72B drafts (Forge)
    }.get(band, Tier.T1A_CODER)
