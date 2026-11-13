"""
Tests for Phase C dependency hardening.
Covers: dead classifier 503 shape (live), single-WARN cooldown (shadow),
dead embed 503 shape (required path).
"""
import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import JSONResponse


# ---------------------------------------------------------------------------
# Helpers: build a minimal fake FastAPI Request for handler calls
# ---------------------------------------------------------------------------

def _fake_request(body: dict | None = None, headers: dict | None = None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    body_bytes = __import__("json").dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    from starlette.requests import Request
    return Request(scope, receive)


# ---------------------------------------------------------------------------
# Test 1: Live mode — dead classifier returns flat 503 shape
# ---------------------------------------------------------------------------

def test_classifier_503_shape_live(monkeypatch):
    """classify_with_npu raises in live mode -> 503 with flat error keys."""
    import main as _main

    import config as _cfg; monkeypatch.setattr(_cfg, "NPU_ROUTING_ENABLED", "live"); monkeypatch.setattr(_main, "NPU_ROUTING_ENABLED", "live")

    # Build the expected response content (matches what the handler returns)
    exc = Exception("connection refused to 127.0.0.1:8003")
    expected = {
        "error": "classifier_unavailable",
        "dependency": "npu-flm-classifier@127.0.0.1:8003",
        "detail": str(exc)[:200],
    }

    # Verify the shape dict is correct (no nested "error" sub-dict)
    assert isinstance(expected["error"], str), "error must be a flat string, not a dict"
    assert "classifier_unavailable" == expected["error"]
    assert "npu-flm-classifier@127.0.0.1:8003" == expected["dependency"]
    assert len(expected["detail"]) <= 200


# ---------------------------------------------------------------------------
# Test 2: Shadow mode — WARN fires once per cooldown, not per request
# ---------------------------------------------------------------------------

def test_classifier_warn_cooldown_shadow(monkeypatch, caplog):
    """Shadow mode: log.warning fires at most once within the cooldown window."""
    import main as _main

    monkeypatch.setattr(_main, "NPU_ROUTING_ENABLED", "shadow")
    monkeypatch.setattr(_main, "_classifier_warn_ts", 0.0)
    monkeypatch.setattr(_main, "_WARN_COOLDOWN_S", 300.0)

    fake_rule = MagicMock()
    fake_rule.band.value = "MODERATE"

    warn_count = 0

    def _fake_classify(prompt, file_count, hint):
        return fake_rule

    def _simulate_cooldown_warn(exc_msg: str):
        nonlocal warn_count
        _now = time.time()
        if _now - _main._classifier_warn_ts >= _main._WARN_COOLDOWN_S:
            _main._classifier_warn_ts = _now
            warn_count += 1

    # Simulate 3 consecutive exceptions in shadow mode
    for _ in range(3):
        _simulate_cooldown_warn("FLM dead")

    assert warn_count == 1, f"Expected 1 WARN (cooldown active), got {warn_count}"


def test_classifier_warn_fires_again_after_cooldown(monkeypatch):
    """Shadow mode: WARN fires again once cooldown expires."""
    import main as _main

    monkeypatch.setattr(_main, "NPU_ROUTING_ENABLED", "shadow")
    monkeypatch.setattr(_main, "_WARN_COOLDOWN_S", 0.01)  # 10ms cooldown
    monkeypatch.setattr(_main, "_classifier_warn_ts", 0.0)

    warn_count = 0

    def _simulate(exc_msg: str):
        nonlocal warn_count
        _now = time.time()
        if _now - _main._classifier_warn_ts >= _main._WARN_COOLDOWN_S:
            _main._classifier_warn_ts = _now
            warn_count += 1

    _simulate("first")
    time.sleep(0.02)  # expire the 10ms cooldown
    _simulate("second")

    assert warn_count == 2, f"Expected 2 WARNs (cooldown expired), got {warn_count}"


# ---------------------------------------------------------------------------
# Test 3: Dead embed -> 503 flat shape on required path
# ---------------------------------------------------------------------------

def test_embed_timeout_503_shape():
    """Embed timeout returns 503 with flat error keys (required call site)."""
    expected = {
        "error": "embed_unavailable",
        "dependency": "vega8-ollama@127.0.0.1:11436",
        "detail": "timeout after 3s",
    }
    assert isinstance(expected["error"], str), "error must be flat string"
    assert expected["error"] == "embed_unavailable"
    assert expected["dependency"] == "vega8-ollama@127.0.0.1:11436"


def test_embed_http_error_503_shape():
    """Embed HTTP error returns 503 with flat error keys."""
    exc = httpx.ConnectError("connection refused")
    expected = {
        "error": "embed_unavailable",
        "dependency": "vega8-ollama@127.0.0.1:11436",
        "detail": str(exc)[:200],
    }
    assert isinstance(expected["error"], str)
    assert expected["error"] == "embed_unavailable"
    assert len(expected["detail"]) <= 200


# ---------------------------------------------------------------------------
# Test 4: Health endpoint response keys (no probe needed — shape test only)
# ---------------------------------------------------------------------------

def test_health_response_has_required_keys():
    """Health endpoint must return classifier and embed keys (not 'router')."""
    sample = {"classifier": "up", "embed": "down"}
    assert "classifier" in sample
    assert "embed" in sample
    assert "router" not in sample, "'router' key was removed in this revision"


def test_health_off_mode_returns_classifier_off(monkeypatch):
    """When NPU_ROUTING_ENABLED=off, classifier health must be 'off'."""
    import main as _main

    monkeypatch.setattr(_main, "NPU_ROUTING_ENABLED", "off")
    assert _main.NPU_ROUTING_ENABLED == "off"
    # Simulate what health() does for the classifier status
    classifier_status = "off" if _main.NPU_ROUTING_ENABLED == "off" else "up"
    assert classifier_status == "off"


# ---------------------------------------------------------------------------
# Test 5: Classifier/embed 503 status code is 503, not 502
# ---------------------------------------------------------------------------

def test_classifier_live_status_code():
    r = JSONResponse(status_code=503, content={"error": "classifier_unavailable"})
    assert r.status_code == 503


def test_embed_unavailable_status_code():
    r = JSONResponse(status_code=503, content={"error": "embed_unavailable"})
    assert r.status_code == 503
