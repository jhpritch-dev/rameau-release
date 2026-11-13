"""
Tests for the Phase 0b tool-aware reroute guard (main.tool_aware_reroute /
main._tier_tool_capability).

Deterministic + offline: exercises the pure reroute decision over the live
capability_registry + fleet_config (no HTTP, no NPU). Operating mode is pinned to
WORKSHOP so get_cascade is stable.
"""
import pytest

import main
from config import Tier, ComplexityBand, OperatingMode, settings


@pytest.fixture(autouse=True)
def _pin_workshop_mode():
    prev = settings.current_mode
    settings.current_mode = OperatingMode.WORKSHOP
    yield
    settings.current_mode = prev


# --------------------------------------------------------------------------
# _tier_tool_capability: known-false / known-true / unknown
# --------------------------------------------------------------------------

def _pin_caps(monkeypatch, mapping, default=None):
    """Pin _tier_tool_capability so guard tests assert LOGIC, not today's config.

    Added 2026-08-07. These tests previously read the live capability_registry for their
    fixtures, so a legitimate config decision (gemma4 tool_calls false->true, "resident eval
    core") falsified four of them at once. The registry's contents are asserted by
    test_capability_registry.py; this file is about the guard's decision.
    """
    monkeypatch.setattr(main, "_tier_tool_capability", lambda t: mapping.get(t, default))


def test_capability_reads_the_registry():
    """Documents CURRENT truth rather than pinning it: gemma4 is tool_calls=true as of
    2026-07-07 -- EMPIRICAL but "~2/3 reliable, OPPORTUNISTIC, NOT a dependable tool fan"
    per the registry note. Whether tools SHOULD route to a 2/3-reliable model is a policy
    question, deliberately left open here."""
    assert main._tier_tool_capability(Tier.T2) is True


def test_capability_devstral_known_true():
    assert main._tier_tool_capability(Tier.T2_5) is True           # devstral:latest (by-model)


def test_capability_unknown_is_none():
    # claude tier has no registry entry -> unknown (None), NOT False.
    assert main._tier_tool_capability(Tier.T3_CLAUDE) is None


# --------------------------------------------------------------------------
# Reroute decisions
# --------------------------------------------------------------------------

def test_reroute_cascade_drops_known_incapable_promotes_capable(monkeypatch):
    _pin_caps(monkeypatch, {Tier.T2: False, Tier.T2_5: True, Tier.T3_CLAUDE: None})
    cascade = [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE]
    new, header, no_capable = main.tool_aware_reroute(cascade, ComplexityBand.COMPLEX, True)
    assert no_capable is False
    assert header == "2->2.5"
    assert new[0] == Tier.T2_5            # known-capable promoted
    assert Tier.T2 not in new             # known-incapable gemma4 dropped
    assert Tier.T3_CLAUDE in new          # unknown kept as fallback


def test_reroute_tier_pin_known_incapable(monkeypatch):
    # Simulates a tier-pinned cascade=[T2] + tools, with T2 pinned KNOWN-incapable.
    _pin_caps(monkeypatch, {Tier.T2: False, Tier.T2_5: True, Tier.T3_CLAUDE: None})
    new, header, no_capable = main.tool_aware_reroute([Tier.T2], ComplexityBand.COMPLEX, True)
    assert no_capable is False
    assert header == "2->2.5"
    assert new[0] == Tier.T2_5


def test_passthrough_when_no_tools():
    new, header, no_capable = main.tool_aware_reroute([Tier.T2], ComplexityBand.COMPLEX, False)
    assert new == [Tier.T2]
    assert header is None
    assert no_capable is False


def test_passthrough_when_primary_already_capable():
    new, header, no_capable = main.tool_aware_reroute([Tier.T2_5, Tier.T2], ComplexityBand.COMPLEX, True)
    assert header is None
    assert no_capable is False
    assert new == [Tier.T2_5, Tier.T2]    # untouched -- intent intact


def test_passthrough_when_primary_unknown():
    # Unknown primary (claude) is NOT rerouted -- guard only acts on KNOWN-incapable.
    new, header, no_capable = main.tool_aware_reroute([Tier.T3_CLAUDE], ComplexityBand.FRONTIER, True)
    assert header is None
    assert no_capable is False
    assert new == [Tier.T3_CLAUDE]


def test_passthrough_empty_cascade():
    new, header, no_capable = main.tool_aware_reroute([], ComplexityBand.COMPLEX, True)
    assert new == []
    assert header is None
    assert no_capable is False


def test_no_tool_capable_in_band_signals_503(monkeypatch):
    # Force a band whose every tier is KNOWN tool-incapable.
    _pin_caps(monkeypatch, {Tier.T2: False, Tier.T1A_FORGE: False})
    monkeypatch.setattr(main, "get_cascade", lambda band: [Tier.T2, Tier.T1A_FORGE])
    new, header, no_capable = main.tool_aware_reroute([Tier.T2], ComplexityBand.COMPLEX, True)
    assert no_capable is True
    assert new == []
    assert header is None
