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
import threading
import time
import uuid
from typing import AsyncIterator

from config import (
    ComplexityBand,
    Tier,
    get_fanout_config,
    get_pipeline_config,
    settings,
)
from review_prompts import (build_review_prompt, get_review_provider,
                            get_review_target, detect_task_type)
from backends.ollama import ollama_chat_completion
from backends.openai_compat import openai_compat_chat_completion
from backends.anthropic import anthropic_chat_completion
import airgap
import egress_guard

log = logging.getLogger("bifrost.two_pass")

# MoA D2 (2026-09-01): this module imported NO telemetry at all, so TWO_PASS, PIPELINE
# and FANOUT were every one of them invisible to inference_events. The import is guarded:
# a telemetry import failure must degrade to 'no records', never to 'no service'.
try:
    from telemetry import (InferenceEvent, PipelineStageEvent,
                           write_event_sync, write_stage_event_sync)
    _TELEMETRY_OK = True
except Exception as _tel_exc:  # pragma: no cover
    _TELEMETRY_OK = False
    log.error("MoA telemetry unavailable: %s: %s", type(_tel_exc).__name__, _tel_exc)

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

PASS_SEPARATOR = "\n\n---\n\n"


def make_sse_chunk(content: str, finish_reason: str | None = None,
                   usage: dict | None = None) -> str:
    """Build an OpenAI-format SSE data line.

    usage is optional and defaults to absent, so the two_pass and fan-out callers are
    unchanged. The pipeline attaches it to its final chunk, mirroring the shape
    backends/ollama.py already emits (F1, 2026-08-28).
    """
    delta = {"content": content} if content else {}
    chunk = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
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


def extract_usage_from_sse(line: str) -> dict | None:
    """Pull a usage block from an SSE data line, if it carries one.

    backends/ollama.py attaches usage to the FINAL chunk, whose delta is empty. The content
    extractor above returns None for exactly that line, so without this the per-stage token
    counts are silently lost and every stage records 0 in / 0 out.
    """
    if not line.startswith("data: "):
        return None
    data_str = line[6:].strip()
    if data_str == "[DONE]":
        return None
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else None


async def collect_sse(gen: AsyncIterator[str], model_label: str) -> dict:
    """Drain an SSE generator into an OpenAI chat.completion object.

    D1 (2026-09-01): the PIPELINE, FANOUT and TWO_PASS branches in main.py returned a
    StreamingResponse unconditionally, so a caller sending "stream": false received raw
    data: frames. That is a contract violation and it was LIVE under v388.

    The content is byte-identical to what a streaming client sees, stage banners included.
    Stripping them here would make the two envelopes carry DIFFERENT TEXT, which is a worse
    inconsistency than the banners are a blemish.
    """
    parts: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    async for line in gen:
        content = extract_content_from_sse(line)
        if content:
            parts.append(content)
            continue
        found = extract_usage_from_sse(line)
        if found:
            usage = {
                "prompt_tokens": int(found.get("prompt_tokens") or 0),
                "completion_tokens": int(found.get("completion_tokens") or 0),
                "total_tokens": int(found.get("total_tokens") or 0),
            }
    return {
        "id": f"chatcmpl-moa-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_label,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(parts)},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


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

# Unclosed code blocks suggest truncation.
#
# A fence LINE is 3+ backticks at the start of a line (optionally indented,
# optionally carrying a language tag). Markdown pairs them, so an ODD count of
# fence lines means one block was opened and never closed.
#
# The previous pattern -- ```[\w]*\n(?!.*```) under re.DOTALL -- asked a different
# question: 'is there no further ``` anywhere after this one'. A CLOSING fence
# satisfies that whenever it is the last fence in the text, so well-formed answers
# were reported as truncated. Measured 2026-09-09 against the shipped object: it
# scored 6/14 on known-answer cases and was wrong on EVERY balanced-fence case,
# including a single closed block at the end of the text whenever the response
# ended with a newline.
#
# Packet: coordination/pending/PACKET-a6-truncation-test-is-inverted-2026-09-04.md
FENCE_LINE_RE = re.compile(r"^[ \t]*`{3,}", re.MULTILINE)


def has_unclosed_code_block(text: str) -> bool:
    """True when the text opened a code fence it never closed.

    Parity, not lookahead: an odd number of fence lines means one block is still
    open. Zero fences is even, so plain prose is never flagged. Counting fence
    LINES rather than any bare ``` keeps triple backticks quoted inside a sentence
    from shifting parity.

    Known limitation, unchanged from the test this replaces: `~~~` fences are not
    counted, so a truncated tilde-fenced block is not detected. Pinned by
    tests/test_truncation_fence_parity.py so the gap is on the record, not hidden.
    """
    if not text:
        return False
    return len(FENCE_LINE_RE.findall(text)) % 2 == 1


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
    if settings.confidence_truncation_penalty and has_unclosed_code_block(draft_text):
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

    review_kind, review_provider, review_model = get_review_target(band)
    review_label = "Local" if review_kind == "local" else "Cloud"
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

    # C123 (2026-10-13): the air gap refuses the cloud review. Checked after the draft and the
    # gate, so a POST /airgap flip during the draft counts; the draft is delivered as final.
    if review_kind != "local" and airgap.air_gapped():
        log.warning(f"AIR_GAPPED_BLOCK: path=two_pass cloud review on {review_provider} refused; "
                    f"draft delivered unreviewed")
        yield make_sse_chunk("\n\n*Cloud review refused: the router is air-gapped. "
                             "The draft above is unreviewed.*\n")
        total_ms = (time.time() - t_start) * 1000
        log.info(
            f"COMPLETE: strategy=TWO_PASS band={band.value} "
            f"draft_tier={local_tier.value} review=AIR_GAPPED total={total_ms:.0f}ms"
        )
        yield make_sse_chunk("", finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    # --- Separator ---
    yield make_sse_chunk(PASS_SEPARATOR)
    yield make_sse_chunk(f"🔍 **{review_label} Review** ({provider_label}):\n\n")

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
                provider=("local" if review_kind == "local" else review_provider),
                task_type=task_type,
            )
            log.info(f"REVIEW: sending to {review_provider} (type={task_type})")

        if review_kind != "local":
            # 2026-10-13 audit: this cloud call never passed main.dispatch_to_tier, the only place
            # cloud-bound messages were masked. Same one-way credential gate and the same C114
            # leak report (counts only; enforcement stays the C114 review's decision), here.
            review_messages, _eg_map, _eg_n = egress_guard.sanitize_messages_secrets(review_messages)
            if _eg_n:
                log.warning(f"EGRESS_GATE: masked {_eg_n} credential(s) before the TWO_PASS cloud review")
            _eg_leak = egress_guard.outbound_leak_report(review_messages, _eg_map)
            if _eg_leak["leaked"] or _eg_leak["residual"]:
                log.error(f"EGRESS_LEAK_CHECK: path=two_pass leaked={_eg_leak['leaked']} "
                          f"residual={_eg_leak['residual']} (counts only)")

        # Dispatch to the right cloud backend
        if review_kind == "local":
            # LOCAL review lane: Forge tier, no outbound cloud call.
            review_stream = await ollama_chat_completion(
                messages=review_messages,
                tier=Tier(review_provider),
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens or 4096,
            )
        elif review_provider == "claude":
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
                model=review_model,
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


# ---------------------------------------------------------------------------
# MoA pipeline -- N-stage generalization of two_pass_stream
# ---------------------------------------------------------------------------

_PIPELINE_OPT_IN_TRUTHY = frozenset({"1", "true", "on", "yes"})


def pipeline_requested(header_value, body_value) -> bool:
    """True only when the caller EXPLICITLY asked for the MoA pipeline.

    Header or body, mirroring the AUTOPILOT declaration at main.py:1784. Absent, empty, or
    any unrecognized value reads as NOT requested: this fails closed by construction, which
    is what makes the no-signal path provably unchanged.
    """
    for v in (header_value, body_value):
        if v is None:
            continue
        if str(v).strip().lower() in _PIPELINE_OPT_IN_TRUTHY:
            return True
    return False


def select_pipeline_stages(band: ComplexityBand) -> list[dict] | None:
    """Return the configured stages when pipeline_mode is enabled and this band qualifies.

    Returns None -- meaning "not a pipeline request" -- when the block is absent, disabled, the
    band is not a trigger band, or the stage list is empty. An absent block reads as disabled, so
    an image built without the block behaves exactly as it does today.
    """
    if _moa_mode() != "pipeline":
        return None
    cfg = get_pipeline_config() or {}
    if not cfg.get("enabled"):
        return None
    if band.value not in (cfg.get("trigger_bands") or []):
        return None
    stages = cfg.get("stages") or None
    if not stages:
        return None
    return list(stages)


def _emit_pipeline_telemetry(request_ref: str, band: ComplexityBand, stages: list[dict],
                             stage_rows: list[dict], gate_reason: str, latency_ms: float,
                             tokens_input: int, tokens_output: int) -> None:
    """Record one pipeline run: N rows in pipeline_stages, one row in inference_events.

    D2 (2026-09-01). Runs on a daemon THREAD, not the event loop and not run_in_executor:
    this is called from a finally block that also executes during generator aclose(), when
    the loop may be tearing the task down and scheduling new coroutines is not dependable.
    Two small INSERTs on a background thread is the boring option and it always runs.

    Swallows everything. Telemetry must never break the call it measures.
    """
    if not _TELEMETRY_OK:
        return

    def _write():
        try:
            tiers = "+".join(str(s.get("tier")) for s in stages)
            for row in stage_rows:
                write_stage_event_sync(PipelineStageEvent(
                    request_ref=request_ref,
                    strategy="PIPELINE",
                    complexity_band=band.value,
                    **row,
                ))
            write_event_sync(InferenceEvent(
                strategy="PIPELINE",
                complexity_band=band.value,
                tier_used=tiers,
                tier_history=[str(s.get("tier")) for s in stages],
                # Stages beyond the first are the pipeline's own escalations. This is NOT a
                # cloud escalation count and no cloud tier can appear here -- the generator
                # holds no cloud backend reference at all.
                escalation_count=max(0, len(stage_rows) - 1),
                model=tiers,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                tokens_total=tokens_input + tokens_output,
                latency_ms=latency_ms,
                cloud_cost_usd=0.0,
                status="PASS" if stage_rows else "ERROR",
            ))
        except Exception as exc:
            log.warning("PIPELINE: telemetry write failed (%s: %s)", type(exc).__name__, exc)

    try:
        threading.Thread(target=_write, name="moa-telemetry", daemon=True).start()
    except Exception as exc:  # pragma: no cover
        log.warning("PIPELINE: telemetry thread failed (%s: %s)", type(exc).__name__, exc)


# Stage N's output is handed to stage N+1 as an assistant turn. With nothing following it that
# is a CONTINUATION prompt, not a refine prompt: the model is asked to keep writing the
# assistant message rather than to improve it. Measured 2026-09-03 over 40 calls against the
# live router -- with an 80-160 char draft the model continues at length (median ~4200 chars,
# 0/16 collapsed), but at 800+ chars it emits 1-4 characters and stops (17/24 collapsed).
# Production drafts run 2779-6547 chars, entirely inside the collapse regime, which is why two
# recorded stage-2 "successes" were 116 and 177 characters.
REFINE_INSTRUCTION = (
    "Revise the draft above into a single improved final answer to the original request. "
    "Correct anything wrong, add what is missing, and cut padding. "
    "Reply with the revised answer only -- no preamble and no commentary about the draft."
)


def _carry_for_next_stage(carried, stage_text, stages, idx):
    """Append the draft AND the instruction telling the next stage to refine it.

    idx is 1-based (the loop uses enumerate(stages, start=1)), so stages[idx] is the NEXT
    stage. A stage may override the wording with an "instruction" key; setting that key to an
    empty string restores the old continuation behavior deliberately rather than by accident.
    """
    out = carried + [{"role": "assistant", "content": stage_text}]
    nxt = stages[idx] if idx < len(stages) else None
    if nxt is None:
        return out
    instr = nxt.get("instruction")
    instr = REFINE_INSTRUCTION if instr is None else str(instr)
    if instr.strip():
        out = out + [{"role": "user", "content": instr.strip()}]
    return out


async def pipeline_stream(
    messages: list[dict],
    band: ComplexityBand,
    stages: list[dict],
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """N-stage local pipeline. Each stage streams live; the EXISTING confidence_gate decides
    whether the next stage runs.

    Deliberately reuses confidence_gate rather than a second scalar confidence notion: two
    confidence notions in one router means thresholds get tuned against the wrong signal. The
    gate_reason string says WHICH test fired, which is what the tuning histogram needs.

    Every stage is a LOCAL ollama tier and this generator holds NO cloud fallback. A pipeline that
    quietly finishes on a paid provider is exactly the failure the free-tier work exists to
    prevent. A stage that fails or returns nothing ends the pipeline on the text produced so far.

    D2 (2026-09-01): every stage is now recorded to pipeline_stages and the run to
    inference_events. Before this a pipeline request wrote NOTHING, and gate_reason lived only
    in pod stdout -- which dies with the pod, and did, taking the v388 evidence with it. The
    emit sits in a finally block because a client disconnect closes this generator, and a run
    that was canceled part-way is exactly the one worth having a record of.
    """
    t_start = time.time()
    request_ref = uuid.uuid4().hex[:16]
    user_prompt = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    carried = list(messages)
    last_text = ""
    stages_run = 0
    gate_reason = "not_evaluated"
    stage_rows: list[dict] = []
    total_in = 0
    total_out = 0

    try:
        for idx, stage in enumerate(stages, start=1):
            try:
                tier = Tier(str(stage.get("tier")))
            except ValueError:
                log.error(f"PIPELINE: stage {idx} names unknown tier {stage.get('tier')!r} -- stopping")
                yield make_sse_chunk(f"\n\n*Pipeline stage {idx} misconfigured: unknown tier*\n")
                gate_reason = "unknown_tier"
                # Recorded like any other stage: a misconfiguration that costs a request is
                # exactly the kind of thing that must not be visible only in stdout.
                stage_rows.append({
                    "stage_index": idx, "stage_count": len(stages),
                    "tier": str(stage.get("tier")),
                    "role": str(stage.get("role") or f"stage{idx}"),
                    "gate_reason": gate_reason, "escalated": 0,
                    "latency_ms": 0.0, "ttft_ms": 0.0, "chars_out": 0,
                    "tokens_input": 0, "tokens_output": 0,
                })
                break

            role = str(stage.get("role") or f"stage{idx}")
            t_stage = time.time()
            first_token = None
            chunks: list[str] = []
            stage_in = 0
            stage_out = 0

            if idx > 1:
                yield make_sse_chunk(PASS_SEPARATOR)
            yield make_sse_chunk(f"**Stage {idx}/{len(stages)} -- {role}** ({tier.value}):\n\n")

            try:
                stage_stream = await asyncio.wait_for(
                    ollama_chat_completion(
                        messages=carried,
                        tier=tier,
                        stream=True,
                        temperature=temperature,
                        max_tokens=max_tokens or 4096,
                    ),
                    timeout=settings.two_pass_local_timeout,
                )
                async for line in stage_stream:
                    content = extract_content_from_sse(line)
                    if content:
                        if first_token is None:
                            first_token = time.time()
                        chunks.append(content)
                        yield make_sse_chunk(content)
                        continue
                    # backends/ollama.py puts usage on the FINAL chunk (F1, 2026-08-28), whose
                    # delta is empty -- so the branch above never saw it and every stage
                    # recorded 0 tokens. Captured here, NOT re-yielded: this generator emits
                    # one usage block of its own once all stages are done.
                    usage = extract_usage_from_sse(line)
                    if usage:
                        stage_in += int(usage.get("prompt_tokens") or 0)
                        stage_out += int(usage.get("completion_tokens") or 0)

            except asyncio.TimeoutError:
                log.warning(
                    f"PIPELINE: stage {idx} ({tier.value}) timed out after "
                    f"{settings.two_pass_local_timeout}s"
                )
                yield make_sse_chunk(
                    f"\n\n*Stage {idx} timed out after {settings.two_pass_local_timeout}s*\n"
                )

            except Exception as e:
                log.warning(f"PIPELINE: stage {idx} ({tier.value}) failed ({type(e).__name__}: {e})")
                yield make_sse_chunk(f"\n\n*Stage {idx} failed: {type(e).__name__}*\n")

            stage_text = "".join(chunks)
            stage_ms = (time.time() - t_stage) * 1000
            ttft = ((first_token - t_stage) * 1000) if first_token else 0
            total_in += stage_in
            total_out += stage_out
            log.info(
                f"PIPELINE: stage={idx} tier={tier.value} role={role} "
                f"chunks={len(chunks)} ttft={ttft:.0f}ms total={stage_ms:.0f}ms "
                f"tok_in={stage_in} tok_out={stage_out}"
            )

            # gate_reason for THIS stage is decided in the branches below. The row is appended
            # now and mutated in place before the loop moves on, so no stage can leave without
            # a real reason -- "incomplete" surviving to the DB would itself be the bug signal.
            row = {
                "stage_index": idx, "stage_count": len(stages), "tier": tier.value,
                "role": role, "gate_reason": "incomplete", "escalated": 0,
                "latency_ms": stage_ms, "ttft_ms": ttft, "chars_out": len(stage_text),
                "tokens_input": stage_in, "tokens_output": stage_out,
            }
            stage_rows.append(row)

            if not stage_text.strip():
                gate_reason = "empty_stage"
                row["gate_reason"] = gate_reason
                log.warning(f"PIPELINE: stage {idx} produced no text -- stopping")
                break

            stages_run = idx
            last_text = stage_text

            if idx == len(stages):
                gate_reason = "final_stage"
                row["gate_reason"] = gate_reason
                break

            if not stage.get("stop_on_confident"):
                gate_reason = "stage_not_gated"
                row["gate_reason"] = gate_reason
                row["escalated"] = 1
                carried = _carry_for_next_stage(carried, stage_text, stages, idx)
                continue

            should_escalate, gate_reason = confidence_gate(stage_text, user_prompt, band)
            row["gate_reason"] = gate_reason
            row["escalated"] = 1 if should_escalate else 0
            log.info(
                f"PIPELINE-GATE: stage={idx} tier={tier.value} "
                f"escalate={should_escalate} reason={gate_reason}"
            )
            if not should_escalate:
                yield make_sse_chunk(f"\n\n*Pipeline stopped at stage {idx} ({gate_reason})*\n")
                break

            carried = _carry_for_next_stage(carried, stage_text, stages, idx)

        total_ms = (time.time() - t_start) * 1000
        log.info(
            f"COMPLETE: strategy=PIPELINE band={band.value} stages_configured={len(stages)} "
            f"stages_run={stages_run} gate={gate_reason} chars={len(last_text)} "
            f"total={total_ms:.0f}ms tok_in={total_in} tok_out={total_out}"
        )

        # Mirrors the final-chunk shape backends/ollama.py already emits, so a non-streaming
        # caller (collect_sse) and a streaming client both see real usage instead of the
        # zeros AUTOPILOT reports and its own comment warns not to trust.
        yield make_sse_chunk("", finish_reason="stop", usage={
            "prompt_tokens": total_in,
            "completion_tokens": total_out,
            "total_tokens": total_in + total_out,
        })
        yield "data: [DONE]\n\n"

    finally:
        _emit_pipeline_telemetry(
            request_ref=request_ref,
            band=band,
            stages=stages,
            stage_rows=stage_rows,
            gate_reason=gate_reason,
            latency_ms=(time.time() - t_start) * 1000,
            tokens_input=total_in,
            tokens_output=total_out,
        )


# ---------------------------------------------------------------------------
# MoA fan-out -- PARALLEL sibling of pipeline_stream
# ---------------------------------------------------------------------------
#
# pipeline_stream is sequential MoA: stage 2 refines stage 1's answer.
# fanout_stream is parallel MoA: N proposers answer the SAME question and the best is selected.
# They are two topologies, not two features, and enabling both is a misconfiguration.


def _moa_mode() -> str | None:
    """Which MoA topology is active: "pipeline", "fanout", or None.

    Mutually exclusive, and it fails LOUD. Silently preferring one is how you get "I enabled
    fanout and got pipeline", so when both are enabled NEITHER runs and the error says why. The
    request then falls through to TWO_PASS / INTERACTIVE, and X-Bifrost-Strategy reports that --
    visible from outside the process, which a log line alone is not.
    """
    pipe = bool((get_pipeline_config() or {}).get("enabled"))
    fan = bool((get_fanout_config() or {}).get("enabled"))
    if pipe and fan:
        log.error(
            "MoA MISCONFIGURED: pipeline_mode and quality_weighted_fanout are BOTH enabled in "
            "fleet_config.json. They are mutually exclusive topologies -- NEITHER will run. "
            "Disable one."
        )
        return None
    if pipe:
        return "pipeline"
    if fan:
        return "fanout"
    return None


def select_fanout_config(band: ComplexityBand) -> dict | None:
    """Return the fan-out config when it is enabled and this band qualifies, else None.

    Requires at least TWO candidates: a fan-out of one is a plain single-tier call wearing a
    selection stage, and it would report a "winner" chosen from a field of one.
    """
    if _moa_mode() != "fanout":
        return None
    cfg = get_fanout_config() or {}
    if band.value not in (cfg.get("trigger_bands") or []):
        return None
    cands = cfg.get("candidates") or []
    if len(cands) < 2:
        return None
    return dict(cfg)


def _normalise(values: list, higher_is_better: bool = True) -> list:
    """Min-max a list of floats to [0, 1]. Missing values score 0.5 (neutral, not zero).

    A missing signal must not be punished as though it were a bad one: scoring None as 0 would
    silently rank any candidate whose signal failed to arrive dead last.
    """
    present = [v for v in values if v is not None]
    if not present:
        return [0.5] * len(values)
    lo, hi = min(present), max(present)
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    out = []
    for v in values:
        if v is None:
            out.append(0.5)
        else:
            n = (v - lo) / (hi - lo)
            out.append(n if higher_is_better else 1.0 - n)
    return out


def score_candidates(cands: list[dict], signals: list, primary_wins_on_tie: bool = True):
    """Rank candidates by the enabled signals. Returns (winner_index, per_candidate_scores).

    Each signal is min-maxed across the field and the enabled ones are averaged with equal
    weight. Equal weighting is a placeholder, not a finding -- it is what you use before you have
    measured which signal predicts quality, and it should be revisited with data, not defended.

    avg_logprob: higher (closer to 0) is better.
    self_eval:   the model's own 0-10 rating of its own answer. A SELF-REPORT. It is implemented
                 because the schema declares it, and it should be validated against human
                 judgment before it is trusted to pick winners.
    """
    if not cands:
        return None, []
    active = [s for s in (signals or []) if s in ("avg_logprob", "self_eval")]
    if not active:
        # No usable signal: the primary wins by construction rather than at random.
        return 0, [{"total": 0.5, "detail": {}} for _ in cands]

    columns = {}
    if "avg_logprob" in active:
        columns["avg_logprob"] = _normalise([c.get("avg_logprob") for c in cands], True)
    if "self_eval" in active:
        columns["self_eval"] = _normalise([c.get("self_eval") for c in cands], True)

    scores = []
    for i in range(len(cands)):
        detail = {k: round(v[i], 4) for k, v in columns.items()}
        scores.append({"total": sum(detail.values()) / len(detail), "detail": detail})

    best = max(s["total"] for s in scores)
    tied = [i for i, s in enumerate(scores) if abs(s["total"] - best) < 1e-9]
    if len(tied) > 1 and primary_wins_on_tie:
        primary = next((i for i, c in enumerate(cands) if c.get("role") == "primary"), 0)
        if primary in tied:
            return primary, scores
    return tied[0], scores


async def _run_candidate(messages: list[dict], entry: dict, temperature: float,
                         max_tokens: int | None, want_logprobs: bool) -> dict | None:
    """Generate ONE candidate, non-streamed. Returns None on any failure.

    Non-streamed on purpose: a winner cannot be chosen until every candidate is whole, and
    logprobs do not survive the native stream translator anyway.
    """
    tier_id = str(entry.get("tier"))
    try:
        tier = Tier(tier_id)
    except ValueError:
        log.error(f"FANOUT: unknown tier {tier_id!r} -- candidate skipped")
        return None
    t0 = time.time()
    try:
        resp = await asyncio.wait_for(
            ollama_chat_completion(
                messages=messages,
                tier=tier,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens or 4096,
                logprobs=want_logprobs,
            ),
            timeout=getattr(settings, "fanout_candidate_timeout", 120),
        )
    except asyncio.TimeoutError:
        log.warning(f"FANOUT: candidate {tier_id} timed out")
        return None
    except Exception as e:
        log.warning(f"FANOUT: candidate {tier_id} failed ({type(e).__name__}: {e})")
        return None

    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not text.strip():
        log.warning(f"FANOUT: candidate {tier_id} returned no text")
        return None
    lp = resp.get("_bifrost_logprobs") or {}
    return {
        "tier": tier_id,
        "role": entry.get("role"),
        "text": text,
        "avg_logprob": lp.get("avg"),
        "min_logprob": lp.get("min"),
        "n_logprobs": lp.get("count", 0),
        "tok_s": resp.get("_bifrost_tok_s"),
        "ms": (time.time() - t0) * 1000,
    }


SELF_EVAL_PROMPT = (
    "Rate the answer you just gave on a scale of 0 to 10 for correctness and completeness. "
    "Reply with the number ONLY, no words."
)


async def _self_eval(messages: list[dict], cand: dict, temperature: float) -> float | None:
    """Ask the candidate's own tier to rate its own answer. One extra call per candidate."""
    try:
        tier = Tier(cand["tier"])
        resp = await asyncio.wait_for(
            ollama_chat_completion(
                messages=messages + [
                    {"role": "assistant", "content": cand["text"]},
                    {"role": "user", "content": SELF_EVAL_PROMPT},
                ],
                tier=tier, stream=False, temperature=temperature, max_tokens=8,
            ),
            timeout=getattr(settings, "fanout_candidate_timeout", 120),
        )
        raw = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        m = re.search(r"\d+(?:\.\d+)?", raw)
        if not m:
            return None
        return max(0.0, min(10.0, float(m.group(0))))
    except Exception as e:
        log.warning(f"FANOUT: self_eval failed for {cand.get('tier')} ({type(e).__name__})")
        return None


async def fanout_stream(
    messages: list[dict],
    band: ComplexityBand,
    cfg: dict,
    temperature: float = 0.7,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Parallel MoA: run every candidate concurrently, score them, stream the winner.

    Every candidate is a LOCAL ollama tier and this generator holds NO cloud fallback -- the same
    property pipeline_stream has, for the same reason.

    Candidates must be CO-RESIDENT to race. That is the real cost difference from the pipeline:
    sequential stages can share VRAM by evicting between stages, concurrent candidates cannot.
    """
    t_start = time.time()
    entries = cfg.get("candidates") or []
    signals = cfg.get("signals") or []
    want_logprobs = "avg_logprob" in signals
    primary_tie = cfg.get("primary_wins_on_tie", True)

    yield make_sse_chunk(
        "**Fan-out** (%d candidates: %s):\n\n"
        % (len(entries), ", ".join(str(e.get("tier")) for e in entries))
    )

    results = await asyncio.gather(
        *[_run_candidate(messages, e, temperature, max_tokens, want_logprobs) for e in entries],
        return_exceptions=False,
    )
    cands = [c for c in results if c]

    if not cands:
        log.error("FANOUT: every candidate failed -- nothing to select from")
        yield make_sse_chunk("\n*Fan-out produced no usable candidate.*\n")
        yield make_sse_chunk("", finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    if "self_eval" in signals:
        evals = await asyncio.gather(*[_self_eval(messages, c, temperature) for c in cands])
        for c, v in zip(cands, evals):
            c["self_eval"] = v

    winner_idx, scores = score_candidates(cands, signals, primary_tie)
    winner = cands[winner_idx]

    for c, s in zip(cands, scores):
        log.info(
            "FANOUT-CAND: tier=%s role=%s chars=%d avg_lp=%s min_lp=%s self_eval=%s "
            "score=%.4f detail=%s ms=%.0f"
            % (c["tier"], c.get("role"), len(c["text"]),
               ("%.4f" % c["avg_logprob"]) if c.get("avg_logprob") is not None else "-",
               ("%.4f" % c["min_logprob"]) if c.get("min_logprob") is not None else "-",
               c.get("self_eval"), s["total"], s["detail"], c["ms"])
        )

    log.info(
        "FANOUT-WINNER: tier=%s role=%s score=%.4f field=%d/%d survived"
        % (winner["tier"], winner.get("role"), scores[winner_idx]["total"],
           len(cands), len(entries))
    )

    yield make_sse_chunk("*Winner: %s*\n\n" % winner["tier"])
    yield make_sse_chunk(winner["text"])

    total_ms = (time.time() - t_start) * 1000
    log.info(
        "COMPLETE: strategy=FANOUT band=%s candidates=%d survived=%d winner=%s "
        "signals=%s total=%.0fms"
        % (band.value, len(entries), len(cands), winner["tier"], signals, total_ms)
    )
    yield make_sse_chunk("", finish_reason="stop")
    yield "data: [DONE]\n\n"
