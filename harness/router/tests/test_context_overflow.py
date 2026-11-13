"""
Tests for CC-CTX-OVERFLOW pre-flight context tripwire (context_overflow.py).
Covers: token estimator, oversized -> 413 shape (local/reject), pressure-band
header + single-WARN cooldown, env=warn -> no 413, env=off disables, and the
check_context wrapper that pulls ctx_len/type from a TIER_BACKENDS entry.

Pure-function tests -- no FastAPI app needed (mirrors test_dependency_hardening).
"""
import json

import pytest

import context_overflow as co
from fastapi.responses import JSONResponse


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """Each test starts with an empty per-alias WARN cooldown map."""
    co._warn_ts.clear()
    yield
    co._warn_ts.clear()


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

def test_estimate_basic_chars_over_four_plus_overhead():
    msgs = [{"role": "user", "content": "x" * 4000}]
    # 4000/4 = 1000, + 1 message * 4 overhead
    assert co.estimate_prompt_tokens(msgs) == 1004


def test_estimate_handles_anthropic_block_content():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "y" * 800}]}]
    # 800/4 = 200, + 1 * 4
    assert co.estimate_prompt_tokens(msgs) == 204


def test_estimate_counts_non_text_blocks_by_json_size():
    block = {"type": "tool_result", "tool_use_id": "abc", "content": "z" * 100}
    msgs = [{"role": "user", "content": [block]}]
    est = co.estimate_prompt_tokens(msgs)
    # json size of the block / 4, + 4 overhead; must be > 0 (not undercounted to 0)
    assert est >= (len(json.dumps(block)) // 4) + 4 - 1


def test_estimate_counts_tools_block():
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "f", "description": "d" * 400}}]
    no_tools = co.estimate_prompt_tokens(msgs)
    with_tools = co.estimate_prompt_tokens(msgs, tools)
    assert with_tools > no_tools


# ---------------------------------------------------------------------------
# Oversized -> 413 shape (local backend, reject mode)
# ---------------------------------------------------------------------------

def test_oversized_local_reject_shape():
    d = co.evaluate(20000, 8192, is_local=True, alias="bifrost-coder-toolmaster", mode="reject")
    assert d["action"] == "reject"
    assert d["body"] == {
        "error": "context_overflow",
        "alias": "bifrost-coder-toolmaster",
        "ctx_len": 8192,
        "estimated_tokens": 20000,
    }
    # flat error key, not a nested dict (matches dependency-hardening convention)
    assert isinstance(d["body"]["error"], str)
    assert d["header"] == ("x-bifrost-ctx-pressure", str(d["pct"]))
    assert d["pct"] > 100


def test_oversized_413_status_code():
    d = co.evaluate(20000, 8192, is_local=True, alias="t", mode="reject")
    r = JSONResponse(status_code=413, content=d["body"])
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Cloud backend over-budget -> warn-only (never 413)
# ---------------------------------------------------------------------------

def test_oversized_cloud_is_warn_not_reject():
    d = co.evaluate(50000, 16384, is_local=False, alias="bifrost-gemini", mode="reject")
    assert d["action"] == "warn"
    assert d["body"] is None


# ---------------------------------------------------------------------------
# env=warn -> no 413 even for local overflow
# ---------------------------------------------------------------------------

def test_mode_warn_local_overflow_no_reject():
    d = co.evaluate(20000, 8192, is_local=True, alias="t", mode="warn")
    assert d["action"] == "warn"
    assert d["body"] is None


def test_mode_off_disables_all_checks():
    d = co.evaluate(999999, 8192, is_local=True, alias="t", mode="off")
    assert d["action"] == "ok"
    assert d["body"] is None
    assert d["header"] is None


# ---------------------------------------------------------------------------
# Pressure band (0.85 <= ratio <= 1.0): header + single-WARN cooldown
# ---------------------------------------------------------------------------

def test_pressure_band_sets_header_and_warns_once():
    # ctx_len 1000, est 900 -> ratio 0.90 -> pressure
    d1 = co.evaluate(900, 1000, is_local=True, alias="p", mode="reject", now=1000.0)
    assert d1["action"] == "pressure"
    assert d1["header"] == ("x-bifrost-ctx-pressure", "90")
    assert d1["warn"] is True  # first WARN fires

    # second call within the 5-min cooldown: still pressure, but no WARN
    d2 = co.evaluate(900, 1000, is_local=True, alias="p", mode="reject", now=1000.0 + 10)
    assert d2["action"] == "pressure"
    assert d2["warn"] is False
    assert d2["msg"] is None


def test_pressure_warn_fires_again_after_cooldown():
    co.evaluate(900, 1000, is_local=True, alias="p", mode="reject", now=1000.0)
    later = co.evaluate(900, 1000, is_local=True, alias="p", mode="reject",
                        now=1000.0 + co.WARN_COOLDOWN_S + 1)
    assert later["warn"] is True


def test_under_threshold_is_ok_no_header():
    d = co.evaluate(400, 1000, is_local=True, alias="t", mode="reject")
    assert d["action"] == "ok"
    assert d["header"] is None


# ---------------------------------------------------------------------------
# check_context wrapper (pulls ctx_len + type from a TIER_BACKENDS entry)
# ---------------------------------------------------------------------------

def test_check_context_local_ollama_reject():
    backend = {"type": "ollama", "num_ctx": 8192, "model": "qwen3-coder:30b-a3b-q4_K_M"}
    msgs = [{"role": "user", "content": "x" * (8192 * 4 * 2)}]  # ~2x ctx
    d = co.check_context(msgs, None, backend, alias="bifrost-coder-toolmaster",
                         default_num_ctx=8192, mode="reject")
    assert d["action"] == "reject"
    assert d["ctx_len"] == 8192
    assert d["estimated_tokens"] > 8192


def test_check_context_uses_default_when_backend_has_no_num_ctx():
    backend = {"type": "ollama", "model": "bifrost-t2-gemma4-16k"}  # no num_ctx
    msgs = [{"role": "user", "content": "x" * 40000}]  # ~10000 tokens
    d = co.check_context(msgs, None, backend, alias="bifrost-t2-gemma4",
                         default_num_ctx=8192, mode="reject")
    assert d["ctx_len"] == 8192  # fell back to default
    assert d["action"] == "reject"


def test_check_context_none_backend_does_not_crash():
    d = co.check_context([{"role": "user", "content": "hi"}], None, None,
                         alias="t", default_num_ctx=8192, mode="reject")
    assert d["action"] == "ok"
    assert d["ctx_len"] == 8192
