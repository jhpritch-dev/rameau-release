"""CEREMONY 123: the air gap also refuses TWO_PASS's cloud review and AUTOPILOT's cloud legs.

main.py enforces the air gap only where it composes a cascade (main.py:2534/2555). TWO_PASS's cloud
review (strategies.two_pass_stream) and AUTOPILOT's legs (subtask_graph._call_tier) reach the cloud
without passing there, and AUTOPILOT's clamp (autopilot_graph._clamp_tier_for_mode) read only the
OFFLINE modes. airgap.air_gapped() gives those modules main's own accessor, read per decision.
Backends are stubbed; nothing leaves the process. The air gap is set the way the router sets it: a
`main` module whose _air_gapped() answers. On the v492 tree the refusal and clamp cases FAIL by
assertion (the cloud is called, the tier is not clamped) and the helper cases fail on import (no
airgap module); the controls pass on both.
"""

import asyncio
import os
import sys
import types

import pytest

import config
from config import OperatingMode

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMPTY = ("", "", 0, 0.0, {})


def _gap(monkeypatch, gapped):
    """The router's own shape: main._air_gapped() answers; the env fallback is cleared."""
    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(_air_gapped=lambda: gapped))
    monkeypatch.delenv("BIFROST_AIR_GAPPED", raising=False)


def _sse(text):
    async def gen():
        if text:
            yield 'data: {"choices":[{"delta":{"content":"%s"}}]}\n\n' % text
        yield "data: [DONE]\n\n"
    return gen()


def _two_pass_out(strategies, msgs):
    async def run():
        return [c async for c in strategies.two_pass_stream(
            messages=msgs, band=config.ComplexityBand.COMPLEX, local_tier=config.Tier.T1B,
            temperature=0.2, max_tokens=64)]
    return "".join(asyncio.run(run()))


def _cloud_review_setup(monkeypatch, strategies, called):
    async def fake_draft(**kw):
        return _sse("a local draft")

    async def fake_cloud(**kw):
        called.append(kw.get("provider", "claude"))
        return _sse("reviewed")

    monkeypatch.setattr(strategies, "ollama_chat_completion", fake_draft)
    monkeypatch.setattr(strategies, "openai_compat_chat_completion", fake_cloud)
    monkeypatch.setattr(strategies, "anthropic_chat_completion", fake_cloud)
    # the live config reviews LOCALLY (local_review_preferred=True); force the cloud branch
    monkeypatch.setattr(strategies, "get_review_target", lambda band, mode=None: ("cloud", "openrouter", "x/y:free"))
    monkeypatch.setattr(strategies, "confidence_gate", lambda draft, prompt, band: (True, "test_low_confidence"))


# --- the helper ----------------------------------------------------------------------------------

def test_helper_reads_mains_accessor_at_call_time(monkeypatch):
    import airgap
    state = {"gapped": False}
    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(_air_gapped=lambda: state["gapped"]))
    assert airgap.air_gapped() is False
    state["gapped"] = True
    assert airgap.air_gapped() is True, "a flip must count from the next call: nothing may be captured"


def test_helper_follows_the_real_main_flag(monkeypatch):
    import airgap
    import main
    monkeypatch.setattr(config.settings, "current_mode", OperatingMode.JARVIS)
    monkeypatch.setattr(main, "_AIR_GAPPED", False)
    assert airgap.air_gapped() is False
    monkeypatch.setattr(main, "_AIR_GAPPED", True)          # what POST /airgap sets
    assert airgap.air_gapped() is True


def test_helper_without_main_uses_the_start_up_rule(monkeypatch):
    import airgap
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delenv("BIFROST_AIR_GAPPED", raising=False)
    monkeypatch.setattr(config.settings, "current_mode", OperatingMode.JARVIS)
    assert airgap.air_gapped() is False
    monkeypatch.setenv("BIFROST_AIR_GAPPED", "true")
    assert airgap.air_gapped() is True
    monkeypatch.delenv("BIFROST_AIR_GAPPED")
    for mode in (OperatingMode.JARVIS_OFFLINE, OperatingMode.WORKSHOP_OFFLINE):
        monkeypatch.setattr(config.settings, "current_mode", mode)
        assert airgap.air_gapped() is True, mode


def test_helper_fails_closed(monkeypatch):
    import airgap

    def broken():
        raise RuntimeError("state unreadable")

    monkeypatch.setitem(sys.modules, "main", types.SimpleNamespace(_air_gapped=broken))
    assert airgap.air_gapped() is True


def test_the_pod_loads_main_as_module_main():
    # airgap finds main's accessor in sys.modules["main"]; that holds because the pod runs `uvicorn main:app`
    with open(os.path.join(REPO, "Dockerfile"), encoding="utf-8") as fh:
        assert '"main:app"' in fh.read()


# --- TWO_PASS ------------------------------------------------------------------------------------

def test_two_pass_cloud_review_refused_when_air_gapped(monkeypatch):
    import strategies
    called = []
    _cloud_review_setup(monkeypatch, strategies, called)
    _gap(monkeypatch, True)
    out = _two_pass_out(strategies, [{"role": "user", "content": "explain the trade-off"}])
    assert called == [], "the TWO_PASS cloud review was dispatched while air-gapped"
    assert "air-gapped" in out
    assert "a local draft" in out, "the draft must still be delivered"
    assert out.rstrip().endswith("data: [DONE]")


def test_two_pass_cloud_review_runs_when_not_air_gapped(monkeypatch):
    import strategies
    called = []
    _cloud_review_setup(monkeypatch, strategies, called)
    _gap(monkeypatch, False)
    out = _two_pass_out(strategies, [{"role": "user", "content": "explain the trade-off"}])
    assert called == ["openrouter"]
    assert "reviewed" in out


def test_two_pass_local_review_ignores_the_air_gap(monkeypatch):
    import strategies
    seen = []

    async def fake_ollama(**kw):
        seen.append(kw.get("tier"))
        return _sse("draft" if kw.get("tier") == config.Tier.T1B else "local review")

    monkeypatch.setattr(strategies, "ollama_chat_completion", fake_ollama)
    monkeypatch.setattr(strategies, "get_review_target", lambda band, mode=None: ("local", "2.5-solo", None))
    monkeypatch.setattr(strategies, "confidence_gate", lambda draft, prompt, band: (True, "test_low_confidence"))
    _gap(monkeypatch, True)
    out = _two_pass_out(strategies, [{"role": "user", "content": "explain the trade-off"}])
    assert seen == [config.Tier.T1B, config.Tier.T2_5_SOLO], "a LOCAL review is not cloud egress"
    assert "local review" in out


# --- AUTOPILOT legs ------------------------------------------------------------------------------

def _free_leg_setup(monkeypatch, called):
    import backends.openai_compat as oc
    import free_tier_budget

    async def fake_oc(**kw):
        called.append("openrouter")
        return {"choices": [{"message": {"content": "done"}}], "usage": {"total_tokens": 2}}

    monkeypatch.setattr(oc, "openai_compat_chat_completion", fake_oc)
    monkeypatch.setattr(free_tier_budget, "get_budget", lambda: None)


def test_autopilot_free_leg_refused_when_air_gapped(monkeypatch):
    import subtask_graph
    called = []
    _free_leg_setup(monkeypatch, called)
    _gap(monkeypatch, True)
    out = asyncio.run(subtask_graph._call_tier("3-synthesis", [{"role": "user", "content": "hi"}]))
    assert called == [], "an AUTOPILOT free cloud leg was dispatched while air-gapped"
    assert out == EMPTY


def test_autopilot_free_leg_runs_when_not_air_gapped(monkeypatch):
    import subtask_graph
    called = []
    _free_leg_setup(monkeypatch, called)
    _gap(monkeypatch, False)
    text, *_ = asyncio.run(subtask_graph._call_tier("3-synthesis", [{"role": "user", "content": "hi"}]))
    assert called == ["openrouter"] and text == "done"


@pytest.mark.parametrize("gapped,expect_call", [(True, False), (False, True)])
def test_autopilot_paid_leg_follows_the_air_gap_when_paid_legs_are_allowed(monkeypatch, gapped, expect_call):
    import subtask_graph
    called = []

    async def fake_anthropic(**kw):
        called.append("anthropic")
        return {"choices": [{"message": {"content": "paid"}}], "usage": {}}

    monkeypatch.setattr(subtask_graph, "anthropic_chat_completion", fake_anthropic)
    monkeypatch.setattr(subtask_graph, "_ALLOW_PAID_CLOUD_LEGS", True)
    _gap(monkeypatch, gapped)
    out = asyncio.run(subtask_graph._call_tier("3-claude", [{"role": "user", "content": "hi"}]))
    assert (called == ["anthropic"]) is expect_call
    if gapped:
        assert out == EMPTY


def test_autopilot_local_leg_ignores_the_air_gap(monkeypatch):
    import subtask_graph
    called = []

    async def fake_ollama(**kw):
        called.append(kw.get("tier"))
        return {"choices": [{"message": {"content": "local"}}], "usage": {}}

    monkeypatch.setattr(subtask_graph, "ollama_chat_completion", fake_ollama)
    _gap(monkeypatch, True)
    text, *_ = asyncio.run(subtask_graph._call_tier("1b", [{"role": "user", "content": "hi"}]))
    assert called and text == "local"


# --- AUTOPILOT clamp -----------------------------------------------------------------------------

@pytest.mark.parametrize("mode,want", [
    (OperatingMode.JARVIS, "2.5"),
    (OperatingMode.WORKSHOP, "1a-coder"),
    (OperatingMode.CLOUD_ONLY, "2.5"),
])
def test_clamp_follows_the_air_gap_flag(monkeypatch, mode, want):
    import autopilot_graph
    monkeypatch.setattr(config.settings, "current_mode", mode)
    _gap(monkeypatch, True)
    assert autopilot_graph._clamp_tier_for_mode("3-synthesis") == want


def test_clamp_leaves_cloud_tiers_alone_without_the_gap(monkeypatch):
    import autopilot_graph
    monkeypatch.setattr(config.settings, "current_mode", OperatingMode.JARVIS)
    _gap(monkeypatch, False)
    assert autopilot_graph._clamp_tier_for_mode("3-synthesis") == "3-synthesis"
    assert autopilot_graph._clamp_tier_for_mode("1b") == "1b"


def test_clamp_offline_modes_unchanged_through_the_real_helper(monkeypatch):
    import autopilot_graph
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delenv("BIFROST_AIR_GAPPED", raising=False)
    for mode, want in ((OperatingMode.JARVIS_OFFLINE, "2.5"), (OperatingMode.WORKSHOP_OFFLINE, "1a-coder")):
        monkeypatch.setattr(config.settings, "current_mode", mode)
        assert autopilot_graph._clamp_tier_for_mode("3-claude") == want, mode
