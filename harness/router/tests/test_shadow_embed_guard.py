"""Tests for shadow_embed_guard (2026-08-24).

Covers the three brakes and the breaker state machine. Deliberately does NOT
touch main.py's shadow paths -- those are fire-and-forget and awkward to assert
on; the guard is the piece with the logic worth pinning down.
"""
import asyncio
import importlib

import pytest

import shadow_embed_guard as seg


@pytest.fixture(autouse=True)
def _fresh():
    """Reset module knobs + guard registry between tests.

    The knobs are read once at import (matching the rest of the router), so tests
    set the module attributes directly rather than round-tripping through env.
    """
    seg.ENABLED = True
    seg.SAMPLE_RATE = 1.0
    seg.MAX_INFLIGHT = 2
    seg.SLOW_MS = 5000.0
    seg.FAIL_THRESHOLD = 3
    seg.COOLDOWN_S = 60.0
    seg.MAX_COOLDOWN_S = 600.0
    seg._GUARDS.clear()
    yield
    seg._GUARDS.clear()


def test_env_parsers_fall_back_on_garbage(monkeypatch):
    monkeypatch.setenv("X_F", "not-a-float")
    monkeypatch.setenv("X_I", "3.9")
    assert seg._env_float("X_F", 1.5) == 1.5
    assert seg._env_int("X_I", 7) == 7
    assert seg._env_int("X_MISSING", 7) == 7


@pytest.mark.parametrize(
    "raw,expected",
    [("0", False), ("false", False), ("FALSE", False), ("no", False),
     ("off", False), ("", False), ("1", True), ("true", True), ("yes", True)],
)
def test_env_bool(monkeypatch, raw, expected):
    monkeypatch.setenv("X_B", raw)
    assert seg._env_bool("X_B", True) is expected


def test_disabled_blocks_everything():
    seg.ENABLED = False
    g = seg.guard("p")
    admitted, reason = g.try_admit()
    assert admitted is False
    assert reason == seg._R_DISABLED


def test_inflight_cap_drops_rather_than_queues():
    seg.MAX_INFLIGHT = 2
    g = seg.guard("p")
    assert g.try_admit()[0] is True
    assert g.try_admit()[0] is True
    admitted, reason = g.try_admit()          # third concurrent call
    assert admitted is False
    assert reason == seg._R_INFLIGHT
    g.release(ok=True, latency_ms=10.0)        # a slot frees
    assert g.try_admit()[0] is True


def test_sample_rate_zero_admits_nothing():
    seg.SAMPLE_RATE = 0.0
    g = seg.guard("p")
    admitted, reason = g.try_admit()
    assert admitted is False
    assert reason == seg._R_SAMPLED


def test_breaker_opens_after_threshold_and_then_costs_the_backend_nothing():
    seg.FAIL_THRESHOLD = 3
    g = seg.guard("p")
    for _ in range(3):
        assert g.try_admit()[0] is True
        g.release(ok=False, latency_ms=10.0)
    assert g.snapshot()["state"] == seg.OPEN
    assert g.snapshot()["breaker_opens"] == 1

    admitted, reason = g.try_admit()
    assert admitted is False
    assert reason == seg._R_BREAKER


def test_success_resets_the_failure_run():
    seg.FAIL_THRESHOLD = 3
    g = seg.guard("p")
    for _ in range(2):
        g.try_admit(); g.release(ok=False, latency_ms=10.0)
    g.try_admit(); g.release(ok=True, latency_ms=10.0)   # resets
    g.try_admit(); g.release(ok=False, latency_ms=10.0)
    assert g.snapshot()["state"] == seg.CLOSED


def test_slow_success_counts_as_failure():
    """The cold-load signature: the call returns 200, just far too late."""
    seg.SLOW_MS = 100.0
    seg.FAIL_THRESHOLD = 2
    g = seg.guard("p")
    for _ in range(2):
        g.try_admit()
        g.release(ok=True, latency_ms=5000.0)
    snap = g.snapshot()
    assert snap["slow"] == 2
    assert snap["state"] == seg.OPEN


def test_half_open_admits_exactly_one_probe_then_closes_on_health():
    seg.FAIL_THRESHOLD = 1
    seg.COOLDOWN_S = 60.0
    g = seg.guard("p")
    g.try_admit(); g.release(ok=False, latency_ms=10.0)
    assert g.snapshot()["state"] == seg.OPEN

    later = __import__("time").monotonic() + 61.0
    assert g.try_admit(now=later)[0] is True         # the probe
    assert g.try_admit(now=later)[0] is False        # no second probe
    assert g.snapshot()["state"] == seg.HALF_OPEN

    g.release(ok=True, latency_ms=10.0)
    assert g.snapshot()["state"] == seg.CLOSED
    assert g.snapshot()["cooldown_s"] == seg.COOLDOWN_S


def test_failed_probe_reopens_with_doubled_cooldown_capped():
    seg.FAIL_THRESHOLD = 1
    seg.COOLDOWN_S = 60.0
    seg.MAX_COOLDOWN_S = 200.0
    g = seg.guard("p")
    g.try_admit(); g.release(ok=False, latency_ms=10.0)

    t = __import__("time").monotonic()
    for expected in (120.0, 200.0, 200.0):           # doubles, then clamps
        t += 10_000.0
        assert g.try_admit(now=t)[0] is True
        g.release(ok=False, latency_ms=10.0)
        assert g.snapshot()["cooldown_s"] == expected
        assert g.snapshot()["state"] == seg.OPEN


def test_admit_context_manager_releases_slot_on_success():
    async def go():
        async with seg.admit("cm") as slot:
            assert slot.admitted is True
        return seg.guard("cm").snapshot()
    snap = asyncio.run(go())
    assert snap["inflight"] == 0
    assert snap["ok"] == 1


def test_admit_context_manager_records_explicit_failure():
    """Both shadow paths fail-open, so the breaker only learns via slot.failed()."""
    seg.FAIL_THRESHOLD = 1
    async def go():
        async with seg.admit("cm") as slot:
            slot.failed("no_signal")
        return seg.guard("cm").snapshot()
    snap = asyncio.run(go())
    assert snap["failed"] == 1
    assert snap["state"] == seg.OPEN


def test_admit_context_manager_releases_and_reraises_on_exception():
    seg.FAIL_THRESHOLD = 1
    async def go():
        with pytest.raises(RuntimeError):
            async with seg.admit("cm") as slot:
                assert slot.admitted
                raise RuntimeError("boom")
        return seg.guard("cm").snapshot()
    snap = asyncio.run(go())
    assert snap["inflight"] == 0
    assert snap["failed"] == 1
    assert snap["state"] == seg.OPEN


def test_dropped_call_body_is_skipped_and_leaks_no_slot():
    seg.ENABLED = False
    ran = []
    async def go():
        async with seg.admit("cm") as slot:
            if not slot.admitted:
                return
            ran.append(1)
    asyncio.run(go())
    assert ran == []
    assert seg.guard("cm").snapshot()["inflight"] == 0


def test_release_never_drives_inflight_negative():
    g = seg.guard("p")
    g.release(ok=True, latency_ms=1.0)   # unmatched release
    assert g.snapshot()["inflight"] == 0


def test_snapshot_all_shape():
    seg.guard("routing_cache").try_admit()
    snap = seg.snapshot_all()
    assert "config" in snap and "paths" in snap
    assert snap["config"]["enabled"] is True
    assert "routing_cache" in snap["paths"]


def test_module_imports_clean_with_bad_env(monkeypatch):
    """A typo'd ConfigMap value must not crash the router at import."""
    monkeypatch.setenv("BIFROST_SHADOW_EMBED_SAMPLE", "banana")
    monkeypatch.setenv("BIFROST_SHADOW_EMBED_MAX_INFLIGHT", "")
    m = importlib.reload(seg)
    assert m.SAMPLE_RATE == 1.0
    assert m.MAX_INFLIGHT == 2
    importlib.reload(seg)   # restore defaults for any later test
