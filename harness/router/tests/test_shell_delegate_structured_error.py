"""
test_shell_delegate_structured_error.py -- Unit tests for F-4 (bifrost_shell.delegate_subtask structured error).

Verifies that when the Router returns an HTTP error (such as 400 TOOL_GATE_BREACH),
bifrost_shell.delegate_subtask retains the HTTP status code and structured error object,
rather than discarding the body into a raw generic string.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SHELL = _ROOT / "bifrost_shell.py"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.syspath_prepend(str(_ROOT))
    spec = importlib.util.spec_from_file_location("bifrost_shell_under_test", _SHELL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Ensure DELEGATE class is enabled in policy
    monkeypatch.setattr(mod, "_POLICY", {"classes": {"DELEGATE": {"enabled": True}}, "tools": {}})
    return mod


def test_delegate_subtask_retains_structured_error(env, monkeypatch):
    """When Router returns HTTP 400 with structured JSON, delegate_subtask retains it."""
    mod = env
    fake_error_payload = {
        "error": {
            "message": "Tool execution denied by policy",
            "type": "bifrost_tool_gate_breach",
            "code": "TOOL_GATE_BREACH",
            "details": {"tool": "rm_rf"},
        }
    }

    mock_resp = httpx.Response(
        status_code=400,
        json=fake_error_payload,
        request=httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions"),
    )

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            mock_resp.raise_for_status()
            return mock_resp

    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    # Ensure safe_leaves returns a safe leaf
    monkeypatch.setattr(mod, "_safe_leaves", lambda: (frozenset(["bifrost-granite"]), ""))

    raw_res = asyncio.run(mod.delegate_subtask("bifrost-granite", "run something"))
    res = json.loads(raw_res)

    assert res["ok"] is False
    assert res.get("status") == 400, f"Expected status 400, got {res.get('status')}"
    err = res.get("error")
    assert isinstance(err, dict), f"Expected error dict, got {type(err)}: {err}"
    assert err.get("code") == "TOOL_GATE_BREACH"
    assert err.get("type") == "bifrost_tool_gate_breach"


def test_non_safe_leaf_refusal_unaffected(env):
    """Non-safe leaf refusal returns RESULT_KEYS and does not make HTTP calls."""
    mod = env
    raw_res = asyncio.run(mod.delegate_subtask("bifrost-auto", "ping"))
    res = json.loads(raw_res)

    assert res["ok"] is False
    assert "not in SAFE_LEAVES" in res["error"]
    assert set(res.keys()) == {"ok", "leaf", "response", "usage", "elapsed_s", "error"}

