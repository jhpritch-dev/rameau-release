"""PENDING-router-decomposer-repoint-igor-2026-09-11: the decomposer runs on Igor (2.5-solo, resident), its think
value is picked per model family, and it is evicted only for profile == "research"."""
import inspect

import pytest

import autopilot_graph as ag


def test_decomposer_tier_is_the_resident_assembler():
    assert ag._DECOMPOSER_TIER == "2.5-solo"
    assert ag._DECOMPOSER_TIER == ag._ASSEMBLER_TIER   # one resident model for decompose + assemble


@pytest.mark.parametrize("model,want", [
    ("gpt-oss-igor-65k", "low"),
    ("GPT-OSS:120b", "low"),
    ("bifrost-panel-gpt-oss-20b-16k:latest", "low"),
    ("bifrost-kat-long", False),
    ("qwen3-coder:30b", False),
    ("bifrost-igor", False),          # an ALIAS (qwen3-coder): the name decides, not the tier
    ("", False),
])
def test_decomposer_think_per_family(monkeypatch, model, want):
    # config.TIER_BACKENDS is a READ-ONLY live view (_LiveTierBackends: no item assignment or deletion), so swap
    # the whole attribute for a plain dict. _decomposer_think imports TIER_BACKENDS at call time, so it reads this.
    import config
    monkeypatch.setattr(config, "TIER_BACKENDS", {"T_DECOMP_TEST": {"model": model, "base_url": "http://x"}})
    assert ag._decomposer_think("T_DECOMP_TEST") == want


def test_live_decomposer_tier_is_gpt_oss_so_think_is_low():
    import config
    model = str((config.TIER_BACKENDS.get(ag._DECOMPOSER_TIER) or {}).get("model") or "")
    assert "gpt-oss" in model.lower(), model
    assert ag._decomposer_think() == "low"


def test_both_decomposer_calls_use_the_family_think():
    src = inspect.getsource(ag)
    assert src.count("tier=_DECOMPOSER_TIER, stream=False, think=_decomposer_think(),") == 1   # checker author
    dec = inspect.getsource(ag._decompose_async)
    assert "think=_decomposer_think()," in dec
    assert "think=False," not in dec


@pytest.mark.parametrize("profile,want", [
    (None, False), ("orchestrator", False), ("coding", False), ("general", False),
    ("research", True), ("RESEARCH", False), ("", False),
])
def test_eviction_only_for_research(profile, want):
    # a decomposer on its OWN tier: the profile rule alone decides
    assert ag._should_evict_decomposer({"profile": profile}, "T_DECOMP", "T_ASSEMBLE") is want


def test_no_profile_key_never_evicts():
    assert ag._should_evict_decomposer({}) is False
    assert ag._should_evict_decomposer(None) is False


def test_research_keeps_the_decomposer_when_it_is_the_assembler():
    # option b (John 2026-09-11): evicting the assembler would lose the keep_alive pin for nothing
    assert ag._should_evict_decomposer({"profile": "research"}, "2.5-solo", "2.5-solo") is False
    # the LIVE layout: Igor is both, so research never evicts today
    assert ag._DECOMPOSER_TIER == ag._ASSEMBLER_TIER
    assert ag._should_evict_decomposer({"profile": "research"}) is False


def test_the_2_qwen_coder_trigger_is_gone():
    dec = inspect.getsource(ag._decompose_async)
    assert '== "2_qwen_coder" for s in subtasks.values()' not in dec
    assert "if _should_evict_decomposer(state):" in dec


def test_run_autopilot_records_profile(monkeypatch):
    seen = {}

    class _Graph:
        def invoke(self, initial, config=None):
            seen.update(initial)
            return dict(initial, status="COMPLETE")

    monkeypatch.setattr(ag, "autopilot_graph", _Graph())
    ag.run_autopilot("Summarize the plan.", profile="research")
    assert seen["profile"] == "research"
    seen.clear()
    ag.run_autopilot("Summarize the plan.")
    assert seen["profile"] is None


@pytest.mark.parametrize("bad", ["bogus", "Research", "paper-harness", 3])
def test_run_autopilot_rejects_unknown_profile(monkeypatch, bad):
    monkeypatch.setattr(ag, "autopilot_graph", None)   # must fail BEFORE the graph runs
    with pytest.raises(ValueError):
        ag.run_autopilot("Summarize the plan.", profile=bad)


def test_state_declares_profile():
    from autopilot_state import AutopilotState
    assert "profile" in AutopilotState.__annotations__
