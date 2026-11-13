"""Per-tier ollama timeout (2026-09-18). The flat 270 s ollama_timeout cut gemma4 (T2) mid-generation;
T2 now gets its own value, every other tier keeps the flat one, and an explicit timeout still wins.

Offline: backends.ollama's httpx.AsyncClient is replaced by a fake that records the timeout it was built with.
"""
import asyncio

import backends.ollama as bo
from config import Tier, settings

_URL = "http://127.0.0.1:9/v1/chat/completions"
_PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _timeout_used(monkeypatch, **kw):
    seen = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "m", "message": {"role": "assistant", "content": "ok"}, "done": True,
                    "done_reason": "stop", "eval_count": 1, "eval_duration": 1_000_000, "prompt_eval_count": 1}

    class _Client:
        def __init__(self, *a, **k):
            seen.append(k.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp()

    monkeypatch.setattr(bo.httpx, "AsyncClient", _Client)
    asyncio.run(bo._request_ollama(_URL, dict(_PAYLOAD), **kw))
    assert len(seen) == 1
    return seen[0]


def test_t2_gets_its_own_timeout(monkeypatch):
    assert settings.ollama_timeout_by_tier.get("2") == 600
    assert _timeout_used(monkeypatch, tier=Tier.T2) == 600


def test_unlisted_tier_keeps_the_flat_timeout(monkeypatch):
    other = next(t for t in Tier if t.value not in settings.ollama_timeout_by_tier)
    assert _timeout_used(monkeypatch, tier=other) == settings.ollama_timeout == 270


def test_no_tier_keeps_the_flat_timeout(monkeypatch):
    assert _timeout_used(monkeypatch) == settings.ollama_timeout


def test_explicit_timeout_wins(monkeypatch):
    assert _timeout_used(monkeypatch, tier=Tier.T2, timeout=42) == 42
