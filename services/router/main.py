"""
BIFROST Complexity Router — Main Application
==============================================
FastAPI service on Bifrost:8080. Intercepts inference requests,
classifies complexity, routes to appropriate tier.

OpenAI-compatible API surface so Continue.dev (and any other
OpenAI-speaking client) works with a single config change:
    apiBase: "http://localhost:11434"  →  "http://localhost:8080"

Phase 0: Ollama direct (no router)
Phase 1: THIS — Router classifies and routes (WORKSHOP mode)
Phase 2: Full tier cascade with Forge
"""

import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

# Ensure project root is on sys.path (uvicorn subprocess may not inherit it)
_project_root = str(Path(__file__).parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from classifier import classify, ClassificationResult
from config import (
    CASCADE_TABLES,
    ComplexityBand,
    DEFAULT_CASCADE_MODE,
    OperatingMode,
    RoutingStrategy,
    Tier,
    TIER_BACKENDS,
    settings,
)
from backends.ollama import ollama_chat_completion, ollama_completion
from backends.anthropic import anthropic_chat_completion
from backends.openai_compat import openai_compat_chat_completion
from strategies import two_pass_stream, select_local_tier
from review_prompts import get_review_provider
from commands import handle_command
from metrics import metrics, MetricsCollector, RoutingEvent


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bifrost.router")


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load API keys from environment if not in settings
    if not settings.anthropic_api_key:
        settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not settings.gemini_api_key:
        settings.gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not settings.groq_api_key:
        settings.groq_api_key = os.environ.get("GROQ_API_KEY")

    cloud_status = []
    if settings.gemini_api_key:
        cloud_status.append("Gemini ✓")
    if settings.groq_api_key:
        cloud_status.append("Groq ✓")
    if settings.anthropic_api_key:
        cloud_status.append("Claude ✓")
    cloud_str = ", ".join(cloud_status) if cloud_status else "NONE"

    log.info("=" * 60)
    log.info("BIFROST Complexity Router starting")
    log.info(f"  Mode:    {settings.current_mode.value}")
    log.info(f"  Profile: {settings.bifrost_profile.value}")
    log.info(f"  Ollama:  {settings.ollama_base_url}")
    log.info(f"  Cloud:   {cloud_str}")
    log.info(f"  Listen:  {settings.host}:{settings.port}")
    log.info("=" * 60)

    # Quick health check on Ollama
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            models = [m["name"] for m in resp.json().get("models", [])]
            log.info(f"  Ollama models available: {models}")
    except Exception as e:
        log.warning(f"  Ollama not reachable: {e}")

    # Initialize Prometheus gauges
    metrics.set_mode(settings.current_mode.value)
    metrics.set_info(
        mode=settings.current_mode.value,
        profile=settings.bifrost_profile.value,
    )
    log.info(f"  Prometheus: /metrics endpoint active")

    # Persistent httpx client for slash commands (Arbiter calls, etc.)
    app.state.http_client = httpx.AsyncClient()

    yield

    await app.state.http_client.aclose()
    log.info("BIFROST Router shutting down")


app = FastAPI(
    title="BIFROST Complexity Router",
    version="1.0.0",
    description="Distributed AI inference routing — local-first, privacy-first",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_cascade(band: ComplexityBand) -> list[Tier]:
    """Get the tier cascade for a complexity band in the current mode."""
    mode = settings.current_mode
    table = CASCADE_TABLES.get(mode, CASCADE_TABLES.get(DEFAULT_CASCADE_MODE, {}))
    return table.get(band, [])


async def dispatch_to_tier(
    tier: Tier,
    messages: list[dict],
    stream: bool,
    temperature: float,
    max_tokens: int | None,
) -> dict | AsyncIterator[str]:
    """Dispatch a request to a specific tier's backend."""
    backend = TIER_BACKENDS[tier]
    backend_type = backend["type"]

    if backend_type == "ollama":
        return await ollama_chat_completion(
            messages=messages,
            tier=tier,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    elif backend_type == "anthropic":
        return await anthropic_chat_completion(
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
        )
    elif backend_type == "openai_compat":
        return await openai_compat_chat_completion(
            messages=messages,
            provider=backend["provider"],
            model=backend.get("model"),
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens or 4096,
        )
    else:
        raise ValueError(f"Unsupported backend type: {backend_type}")


def extract_user_prompt(messages: list[dict]) -> str:
    """Extract the last user message for classification."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content", "")
    return ""


def add_routing_headers(response: Response, band: ComplexityBand, tier: Tier, classification: ClassificationResult):
    """Add X-Bifrost-* headers to response."""
    response.headers["X-Bifrost-Band"] = band.value
    response.headers["X-Bifrost-Tier"] = tier.value
    response.headers["X-Bifrost-Mode"] = settings.current_mode.value
    response.headers["X-Bifrost-Confidence"] = str(round(classification.confidence, 2))


# ---------------------------------------------------------------------------
# OpenAI-compatible endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    This is what Continue.dev hits for chat and inline edit.
    """
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens")
    model_hint = body.get("model", "")

    # --- Slash command interception ---
    user_prompt = extract_user_prompt(messages)
    if user_prompt.startswith("/"):
        cmd_result = await handle_command(
            user_prompt,
            settings,
            metrics,
            app.state.http_client,
        )
        if cmd_result is not None:
            log.info(f"COMMAND: {user_prompt.split()[0]}")
            return JSONResponse({
                "id": f"cmd-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "bifrost-system",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": cmd_result},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
    # --- End slash command interception ---

    # Extract classification inputs
    hint = request.headers.get("X-Complexity-Hint")
    file_count = int(request.headers.get("X-File-Count", "0"))

    # Classify
    classification = classify(prompt=user_prompt, file_count=file_count, hint=hint)
    band = classification.band
    cascade = get_cascade(band)

    if settings.log_routing_decisions:
        log.info(
            f"ROUTE: band={band.value} confidence={classification.confidence:.2f} "
            f"cascade={[t.value for t in cascade]} "
            f"reason='{classification.reasoning}'"
        )

    if not cascade:
        # No tiers available for this band (e.g., COMPLEX in WORKSHOP_OFFLINE)
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": (
                        f"No tiers available for {band.value} in {settings.current_mode.value} mode. "
                        f"Task requires human intervention or mode upgrade."
                    ),
                    "type": "bifrost_routing_error",
                    "code": "FAILED_NEEDS_HUMAN",
                    "band": band.value,
                    "mode": settings.current_mode.value,
                }
            },
        )

    # --- Strategy branching ---
    strategy = settings.default_strategy
    min_band_for_two_pass = ComplexityBand(settings.two_pass_min_band)
    band_order = [ComplexityBand.TRIVIAL, ComplexityBand.MODERATE, ComplexityBand.COMPLEX, ComplexityBand.FRONTIER]

    use_two_pass = (
        strategy == RoutingStrategy.TWO_PASS
        and band_order.index(band) >= band_order.index(min_band_for_two_pass)
    )

    if use_two_pass:
        # --- TWO_PASS: stream draft live, then stream cloud review ---
        local_tier = select_local_tier(band)
        review_provider = get_review_provider(band)
        log.info(
            f"TWO_PASS: band={band.value} draft={local_tier.value} "
            f"review={review_provider}"
        )

        t_start = time.time()

        async def two_pass_with_metrics():
            async for chunk in two_pass_stream(
                messages=messages,
                band=band,
                local_tier=local_tier,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                yield chunk
            # Record metrics after full pipeline completes
            total_ms = (time.time() - t_start) * 1000
            metrics.record(RoutingEvent(
                timestamp=time.time(),
                band=band,
                tier=local_tier,
                latency_ms=total_ms,
                success=True,
            ))

        return StreamingResponse(
            two_pass_with_metrics(),
            media_type="text/event-stream",
            headers={
                "X-Bifrost-Band": band.value,
                "X-Bifrost-Tier": f"{local_tier.value}+{review_provider}",
                "X-Bifrost-Mode": settings.current_mode.value,
                "X-Bifrost-Strategy": "TWO_PASS",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # --- INTERACTIVE strategy: try first available tier in cascade ---
    # (Full cascade with escalation is AUTOPILOT — Phase 3)
    last_error = None
    for tier in cascade:
        t_start = time.time()
        try:
            result = await dispatch_to_tier(
                tier=tier,
                messages=messages,
                stream=stream,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            latency_ms = (time.time() - t_start) * 1000
            escalated = tier != cascade[0]

            if stream:
                # Streaming — measure real latency inside the generator
                async def stream_with_headers():
                    first_token_time = None
                    async for chunk in result:
                        if first_token_time is None:
                            first_token_time = time.time()
                        yield chunk
                    # Record metrics after stream completes with real TTFT
                    total_ms = (time.time() - t_start) * 1000
                    ttft_ms = ((first_token_time - t_start) * 1000) if first_token_time else 0
                    metrics.record(RoutingEvent(
                        timestamp=time.time(),
                        band=band,
                        tier=tier,
                        latency_ms=ttft_ms,
                        success=True,
                        escalated=escalated,
                        escalation_from=cascade[0] if escalated else None,
                    ))
                    log.info(
                        f"COMPLETE: tier={tier.value} ttft={ttft_ms:.0f}ms total={total_ms:.0f}ms"
                    )

                resp = StreamingResponse(
                    stream_with_headers(),
                    media_type="text/event-stream",
                    headers={
                        "X-Bifrost-Band": band.value,
                        "X-Bifrost-Tier": tier.value,
                        "X-Bifrost-Mode": settings.current_mode.value,
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
                return resp
            else:
                # Non-streaming — latency is real since we awaited the full response
                metrics.record(RoutingEvent(
                    timestamp=time.time(),
                    band=band,
                    tier=tier,
                    latency_ms=latency_ms,
                    success=True,
                    escalated=escalated,
                    escalation_from=cascade[0] if escalated else None,
                ))
                response = JSONResponse(content=result)
                add_routing_headers(response, band, tier, classification)
                return response

        except Exception as e:
            last_error = e
            latency_ms = (time.time() - t_start) * 1000
            log.warning(
                f"ESCALATE: tier={tier.value} failed ({type(e).__name__}: {e}), "
                f"trying next in cascade"
            )
            metrics.record(RoutingEvent(
                timestamp=time.time(),
                band=band,
                tier=tier,
                latency_ms=latency_ms,
                success=False,
                escalated=True,
            ))
            continue

    # All tiers exhausted
    log.error(f"FAILED: All tiers exhausted for band={band.value}. Last error: {last_error}")
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "message": f"All tiers failed for {band.value}. Last error: {str(last_error)}",
                "type": "bifrost_cascade_exhausted",
                "code": "CASCADE_EXHAUSTED",
                "band": band.value,
                "tiers_tried": [t.value for t in cascade],
            }
        },
    )


@app.post("/v1/completions")
async def completions(request: Request):
    """
    OpenAI-compatible completions endpoint.
    Used by Continue.dev for tab autocomplete (FIM).
    Always routes to Tier 1a-coder for speed.
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)
    temperature = body.get("temperature", 0.2)
    max_tokens = body.get("max_tokens", 256)

    tier = Tier.T1A_CODER
    band = ComplexityBand.TRIVIAL

    t_start = time.time()
    try:
        result = await ollama_completion(
            prompt=prompt,
            tier=tier,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        latency_ms = (time.time() - t_start) * 1000
        metrics.record(RoutingEvent(
            timestamp=time.time(),
            band=band,
            tier=tier,
            latency_ms=latency_ms,
            success=True,
        ))

        if stream:
            return StreamingResponse(
                result,
                media_type="text/event-stream",
                headers={
                    "X-Bifrost-Band": "TRIVIAL",
                    "X-Bifrost-Tier": "1a-coder",
                    "Cache-Control": "no-cache",
                },
            )
        else:
            response = JSONResponse(content=result)
            response.headers["X-Bifrost-Band"] = "TRIVIAL"
            response.headers["X-Bifrost-Tier"] = "1a-coder"
            return response

    except Exception as e:
        log.error(f"Autocomplete failed: {e}")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "bifrost_error"}},
        )


# ---------------------------------------------------------------------------
# Model listing (Continue.dev queries this)
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def list_models():
    """
    OpenAI-compatible model listing. Exposes BIFROST routing
    as virtual models that Continue.dev can select.
    """
    mode = settings.current_mode.value
    models = [
        {
            "id": "bifrost-auto",
            "object": "model",
            "owned_by": "bifrost",
            "description": f"Auto-routed by complexity ({mode} mode)",
        },
        {
            "id": "bifrost-coder",
            "object": "model",
            "owned_by": "bifrost",
            "description": "Direct to Tier 1a-coder (qwen2.5-coder:7b)",
        },
        {
            "id": "bifrost-cloud",
            "object": "model",
            "owned_by": "bifrost",
            "description": "Direct to Tier 3-Claude",
        },
    ]
    return {"object": "list", "data": models}


# ---------------------------------------------------------------------------
# BIFROST-specific endpoints
# ---------------------------------------------------------------------------

@app.get("/status")
async def status():
    """BIFROST system status."""
    return {
        "router": "online",
        "mode": settings.current_mode.value,
        "profile": settings.bifrost_profile.value,
        "strategy": settings.default_strategy.value,
        "cloud_configured": {
            "groq": bool(settings.groq_api_key),
            "gemini": bool(settings.gemini_api_key),
            "anthropic": bool(settings.anthropic_api_key),
        },
        "two_pass": {
            "active_reviewers": {
                "MODERATE": get_review_provider(ComplexityBand.MODERATE),
                "COMPLEX": get_review_provider(ComplexityBand.COMPLEX),
                "FRONTIER": get_review_provider(ComplexityBand.FRONTIER),
            },
            "min_band": settings.two_pass_min_band,
            "local_timeout": settings.two_pass_local_timeout,
        },
        "metrics": metrics.summary(),
    }


@app.post("/strategy")
async def set_strategy(request: Request):
    """Switch routing strategy: INTERACTIVE, TWO_PASS, or AUTOPILOT."""
    body = await request.json()
    strategy_str = body.get("strategy", "").upper()

    try:
        new_strategy = RoutingStrategy(strategy_str)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"Invalid strategy: {strategy_str}. "
                f"Use: {', '.join(s.value for s in RoutingStrategy)}"
            },
        )

    old = settings.default_strategy
    settings.default_strategy = new_strategy
    log.info(f"STRATEGY: {old.value} -> {new_strategy.value}")

    return {
        "strategy": new_strategy.value,
        "previous": old.value,
        "two_pass_config": {
            "active_reviewers": {
                "MODERATE": get_review_provider(ComplexityBand.MODERATE),
                "COMPLEX": get_review_provider(ComplexityBand.COMPLEX),
                "FRONTIER": get_review_provider(ComplexityBand.FRONTIER),
            },
            "min_band": settings.two_pass_min_band,
        },
    }


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus exposition format endpoint — scraped by Hearth Prometheus."""
    body, content_type = metrics.prometheus_export()
    return Response(content=body, media_type=content_type)


@app.get("/metrics/summary")
async def metrics_summary():
    """Routing metrics summary."""
    return metrics.summary()


@app.get("/metrics/recent")
async def metrics_recent(n: int = 20):
    """Recent routing events."""
    return metrics.recent(n)


@app.get("/cascade/{band}")
async def show_cascade(band: str):
    """Show the cascade for a given complexity band in current mode."""
    try:
        b = ComplexityBand(band.upper())
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid band: {band}. Use TRIVIAL/MODERATE/COMPLEX/FRONTIER"},
        )
    cascade = get_cascade(b)
    return {
        "band": b.value,
        "mode": settings.current_mode.value,
        "cascade": [t.value for t in cascade],
        "backends": [
            {"tier": t.value, **TIER_BACKENDS[t]}
            for t in cascade
        ],
    }


@app.post("/classify")
async def classify_request(request: Request):
    """Debug endpoint: classify a prompt without routing."""
    body = await request.json()
    prompt = body.get("prompt", "")
    hint = body.get("hint")
    file_count = body.get("file_count", 0)

    result = classify(prompt=prompt, file_count=file_count, hint=hint)
    cascade = get_cascade(result.band)

    return {
        "band": result.band.value,
        "confidence": round(result.confidence, 3),
        "reasoning": result.reasoning,
        "scores": result.scores,
        "cascade": [t.value for t in cascade],
        "mode": settings.current_mode.value,
    }


@app.post("/mode")
async def set_mode(request: Request):
    """
    Manually override operating mode.
    In production, the Arbiter sets this via Broadcaster state.
    For Phase 1 testing, manual override is useful.
    """
    body = await request.json()
    mode_str = body.get("mode", "").upper()
    try:
        new_mode = OperatingMode(mode_str)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid mode: {mode_str}. Valid: {[m.value for m in OperatingMode]}"},
        )

    old_mode = settings.current_mode
    settings.current_mode = new_mode
    log.info(f"MODE CHANGE: {old_mode.value} → {new_mode.value}")

    return {
        "previous": old_mode.value,
        "current": new_mode.value,
        "cascade_tables": {
            band.value: [t.value for t in get_cascade(band)]
            for band in ComplexityBand
        },
    }


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "bifrost-router"}


@app.get("/")
async def root():
    return {
        "service": "BIFROST Complexity Router",
        "version": "1.0.0",
        "mode": settings.current_mode.value,
        "docs": "/docs",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info",
    )
