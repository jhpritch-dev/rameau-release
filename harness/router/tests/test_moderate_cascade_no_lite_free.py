"""CEREMONY 118 (2026-10-13), John: "remove 3-lite-free from the MODERATE cascade".

Reads CASCADE_TABLES directly for every mode (get_cascade() reads settings.current_mode, which in
a test process is the class default, not the mode the router runs in), and drives the real
escalation node so no fallback path can still hand a MODERATE leg to the free overflow tier.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CASCADE_TABLES, ComplexityBand, OperatingMode, Tier, settings  # noqa: E402
import subtask_graph as SG  # noqa: E402
from test_degenerate_escalation import FakeSpec, make_state  # noqa: E402

MODES = list(CASCADE_TABLES)

# The chains as signed in Ceremony 118. Written out so that adding a tier back is a visible
# decision here, not a side effect.
EXPECTED_MODERATE = {
    OperatingMode.JARVIS:           [Tier.T1B, Tier.T2, Tier.T3_FAST],
    OperatingMode.WORKSHOP:         [Tier.T1B, Tier.T2],
    OperatingMode.WORKSHOP_OFFLINE: [Tier.T1B, Tier.T2],
    OperatingMode.CLOUD_PLUS:       [Tier.T1A_HEARTH, Tier.T3_FAST],
    OperatingMode.CLOUD_ONLY:       [Tier.T3_FAST],
    OperatingMode.WORKSTATION:      [Tier.T1B, Tier.T2],
    OperatingMode.REMOTE:           [Tier.T1B, Tier.T2, Tier.T3_FAST],
    OperatingMode.NOMAD:            [Tier.T1B, Tier.T2, Tier.T3_FAST],
    OperatingMode.JARVIS_OFFLINE:   [Tier.T1B, Tier.T2],
}


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.value)
def test_no_moderate_chain_carries_3_lite_free(mode):
    assert Tier.T3_LITE_FREE not in CASCADE_TABLES[mode][ComplexityBand.MODERATE], mode


def test_every_mode_is_pinned():
    assert set(EXPECTED_MODERATE) == set(MODES), sorted(m.value for m in set(MODES) ^ set(EXPECTED_MODERATE))


@pytest.mark.parametrize("mode", MODES, ids=lambda m: m.value)
def test_moderate_chain_is_the_signed_one(mode):
    assert CASCADE_TABLES[mode][ComplexityBand.MODERATE] == EXPECTED_MODERATE[mode]


def test_other_bands_are_untouched():
    # Scope: MODERATE only. FRONTIER keeps 3-lite-free in every mode that had it.
    for mode in (OperatingMode.JARVIS, OperatingMode.WORKSHOP, OperatingMode.WORKSTATION,
                 OperatingMode.REMOTE, OperatingMode.NOMAD):
        assert CASCADE_TABLES[mode][ComplexityBand.FRONTIER] == [
            Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING], mode


_HAD_LITE_FREE = [m for m in MODES if m not in (OperatingMode.WORKSHOP_OFFLINE, OperatingMode.JARVIS_OFFLINE)]
_ENDS_ON_CLOUD = [m for m in _HAD_LITE_FREE if EXPECTED_MODERATE[m][-1].value.startswith("3-")]


def _escalate_from_the_chain_top(monkeypatch, mode, deliverable):
    monkeypatch.setattr(settings, "current_mode", mode)
    chain = [t.value for t in EXPECTED_MODERATE[mode]]
    spec = FakeSpec(tier=chain[-1], deliverable=deliverable)
    out = SG.escalate_subtask_node(make_state(spec, output="wrong", attempt=1, tier_history=chain))
    return out.get("spec"), out.get("error")


# Measured 2026-10-13 before the change: with the rest of the chain burned and the leg on its top
# tier, every one of these went to 3-lite-free. These are the definite cases; the next test
# covers the code leg that took the local-coder detour.
@pytest.mark.parametrize("mode", _HAD_LITE_FREE, ids=lambda m: m.value)
def test_docs_leg_at_the_chain_top_does_not_reach_3_lite_free(monkeypatch, mode):
    new, err = _escalate_from_the_chain_top(monkeypatch, mode, "docs")
    assert new is None or new.assigned_tier != "3-lite-free", (mode, err)


@pytest.mark.parametrize("mode", _ENDS_ON_CLOUD, ids=lambda m: m.value)
def test_code_leg_on_the_cloud_top_does_not_reach_3_lite_free(monkeypatch, mode):
    new, err = _escalate_from_the_chain_top(monkeypatch, mode, "code_selftest")
    assert new is None or new.assigned_tier != "3-lite-free", (mode, err)


_EVERY_MODERATE_TIER_TRIED = ["1a-hearth", "1b", "2", "2.5", "2_qwen_coder", "3-fast"]


# Only the four modes where this input reached 3-lite-free before the change (measured
# 2026-10-13); in JARVIS/REMOTE/NOMAD it did not, so including them would pass vacuously.
@pytest.mark.parametrize("mode", [OperatingMode.WORKSHOP, OperatingMode.CLOUD_PLUS,
                                  OperatingMode.CLOUD_ONLY, OperatingMode.WORKSTATION],
                         ids=lambda m: m.value)
@pytest.mark.parametrize("deliverable", ["docs", "code_selftest"])
def test_escalation_never_reaches_3_lite_free_after_the_detour(monkeypatch, mode, deliverable):
    # The band-terminal fallback scans the chain; with every other MODERATE tier burned, the
    # free overflow tier is what it used to pick. It must find nothing to escalate to now.
    monkeypatch.setattr(settings, "current_mode", mode)
    spec = FakeSpec(tier="2", deliverable=deliverable, band="MODERATE")
    out = SG.escalate_subtask_node(make_state(spec, output="wrong", attempt=1,
                                              tier_history=list(_EVERY_MODERATE_TIER_TRIED)))
    new = out.get("spec")
    assert new is None or new.assigned_tier != "3-lite-free", (mode, out.get("error"))


@pytest.mark.parametrize("mode", [OperatingMode.WORKSHOP, OperatingMode.WORKSTATION],
                         ids=lambda m: m.value)
def test_workshop_docs_leg_on_tier_2_has_no_cloud_escalation(monkeypatch, mode):
    # Before 118 this leg went to 3-lite-free. Tier 2 is the band top again, as offline.
    monkeypatch.setattr(settings, "current_mode", mode)
    spec = FakeSpec(tier="2", deliverable="docs")
    out = SG.escalate_subtask_node(make_state(spec, output="wrong", attempt=1, tier_history=["1b", "2"]))
    new = out.get("spec")
    assert new is None or not new.assigned_tier.startswith("3-"), (mode, new and new.assigned_tier)
