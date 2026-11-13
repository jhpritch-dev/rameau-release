"""Task #56 -- pin the chain-end code hop and the degenerate-run circuit breaker.

Both defects were measured in the v327 bank (demobank-2/s2), not imagined:
  - a MODERATE `code_selftest` leg reached tier 2 and had nowhere to escalate, because
    CASCADE_TABLES ends MODERATE at T2 in every mode and tier 2 is a prose model;
  - tier 2 then returned 310 chars of 100% `<unused24>` on three consecutive attempts and
    the router re-rolled it each time.

These tests assert the FIX, and equally that the fix does not overreach: a docs leg must
never be handed to a coder, and a single degenerate roll must still get a retry.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    CASCADE_TABLES, ComplexityBand, OperatingMode, Tier, settings,
)
from subtask_graph import (  # noqa: E402
    _CODE_CHAIN_END_HOP,
    _CODER_CROSS_ESCALATION,
    _MAX_CONSECUTIVE_DEGENERATE,
    _trailing_degenerate_runs,
    escalate_subtask_node,
    get_cascade,
)

DEGEN = "<unused24>" * 31  # the measured shape


class FakeSpec:
    """Minimal stand-in. escalate_subtask_node only reads attributes + model_dump()."""

    def __init__(self, tier="2", deliverable="code_selftest", band="MODERATE"):
        self.type = "subtask_spec"
        self.graph_id = "t56"
        self.subtask_id = "s2"
        self.description = "Implement top_n_words(text, n) AND its pytest suite."
        self.acceptance_criteria = ["meets the spec"]
        self.assigned_tier = tier
        self.complexity_band = band
        self.dependencies = []
        self.context_budget_tokens = 8192
        self.attempt_number = 0
        self.max_attempts = 3
        self.think = False
        self.partial_output = None
        self.known_dead_ends = []
        self.deliverable = deliverable
        self.function_name = "top_n_words"
        self.input_signature = "text: str, n: int"
        self.output_signature = "list[tuple[str,int]]"
        self.dep_context = None
        self.contract_dep = None
        self.acceptance = None

    def model_dump(self):
        return dict(self.__dict__)


class FakeResult:
    def __init__(self, output=""):
        self.output = output
        self.escalation_artifact = None
        self.status = "FAIL"


def make_state(spec, output="", accumulator=None, attempt=1, tier_history=None):
    # tier_history is what the tried-tier guards read since a0f7322 (A2); context_accumulator is
    # the token-budget structure and is deliberately NOT consulted. Seed tried tiers HERE.
    return {
        "spec": spec,
        "result": FakeResult(output),
        "attempt_count": attempt,
        "tier_history": tier_history if tier_history is not None else ["1b", "2"],
        "context_accumulator": accumulator if accumulator is not None else [],
        "verification_level": 5,
        "error": None,
    }


# --------------------------------------------------------------- the premise of the fix
def test_moderate_band_really_does_end_at_tier_2():
    """If this ever fails the hop is obsolete -- delete it rather than keeping both.

    2026-09-10: 15b0a84 appended cloud tiers after T2, so the premise is now about the last LOCAL
    tier. The hop is not obsolete: the *_OFFLINE bands still end at 2, and online the
    local-coder-before-cloud block consumes the same map.
    """
    chain = get_cascade("MODERATE")
    local = [t for t in chain if not t.startswith("3-")]
    assert local[-1] == "2", chain
    assert "2.5" not in chain, "2.5 reachable in MODERATE now -- revisit _CODE_CHAIN_END_HOP"


# WORKSHOP dropped 2026-09-10: 15b0a84 gave it a terminal cloud leg, as 870b4c2 did JARVIS.
@pytest.mark.parametrize("mode", [OperatingMode.WORKSHOP_OFFLINE, OperatingMode.JARVIS_OFFLINE])
def test_moderate_ends_at_2_in_every_mode_we_actually_run(mode):
    """get_cascade() reads settings.current_mode, so a test process silently asserts against
    whatever config.py's DEFAULT happens to be -- not the mode the router is really in.

    Measured 2026-08-10: probing settings.current_mode in a SEPARATE process inside the pod
    returned WORKSHOP (the class default) while the running server was in JARVIS. The two
    bands happen to be identical here, so nothing was wrong -- but the test was passing by
    coincidence, not by assertion. Read the table directly instead, for every mode the fleet
    actually runs, so this cannot go quietly stale.
    """
    band = CASCADE_TABLES[mode][ComplexityBand.MODERATE]
    assert band[-1] == Tier.T2, (mode, band)
    assert Tier.T2_5 not in band, f"{mode.value} MODERATE reaches 2.5 -- revisit the hop"


def test_hop_table_is_disjoint_from_the_unGated_cross_map():
    """The two maps must not overlap: the cross map is consulted WITHOUT a deliverable check."""
    assert not (set(_CODE_CHAIN_END_HOP) & set(_CODER_CROSS_ESCALATION))


# --------------------------------------------------------------- A: the chain-end code hop
def test_code_leg_at_chain_end_hops_to_the_coder():
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output="def f(): pass"))
    assert out.get("error") is None
    assert out["spec"].assigned_tier == "2.5", "code leg should reach qwen3-coder"


@pytest.mark.parametrize("kind", ["code", "tests", "code_selftest"])
def test_every_code_deliverable_gets_the_hop(kind):
    spec = FakeSpec(tier="2", deliverable=kind)
    out = escalate_subtask_node(make_state(spec, output="def f(): pass"))
    assert out["spec"].assigned_tier == "2.5"


@pytest.mark.parametrize("kind", ["docs", "analysis", "prose", ""])
def test_non_code_leg_is_NOT_handed_to_a_coder(kind):
    """The whole reason this is a separate, gated table."""
    spec = FakeSpec(tier="2", deliverable=kind)
    out = escalate_subtask_node(make_state(spec, output="Some prose."))
    tier = out["spec"].assigned_tier if out.get("spec") else None
    assert tier != "2.5", f"deliverable={kind!r} must not land on a coder"


def test_hop_is_one_shot_not_a_pingpong():
    """Already-tried tiers are excluded, so the leg cannot bounce back into 2.5."""
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    acc = [{"tier": "2.5", "attempt": 1, "degenerate": False}]
    out = escalate_subtask_node(make_state(spec, output="def f(): pass", accumulator=acc,
                                           tier_history=["1b", "2", "2.5", "2"]))
    tier = out["spec"].assigned_tier if out.get("spec") else None
    assert tier != "2.5", "2.5 already tried -- must not be re-selected"


def test_tried_guard_reads_tier_history_not_the_accumulator():
    """A2 (a0f7322): the accumulator is truncated for the token budget, so it is NOT the record of
    tried tiers. 2.5 present ONLY in the accumulator must not block the hop."""
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    acc = [{"tier": "2.5", "attempt": 1, "degenerate": False}]
    out = escalate_subtask_node(make_state(spec, output="def f(): pass", accumulator=acc))
    assert out["spec"].assigned_tier == "2.5"


# --------------------------------------------------------------- B: the run counter
def test_counter_counts_an_unbroken_run():
    st = make_state(FakeSpec(), accumulator=[
        {"tier": "2", "degenerate": True},
        {"tier": "2", "degenerate": True},
    ])
    assert _trailing_degenerate_runs(st, "2") == 2


def test_counter_resets_on_a_real_answer():
    """A merely-wrong answer between two degenerate rolls breaks the run."""
    st = make_state(FakeSpec(), accumulator=[
        {"tier": "2", "degenerate": True},
        {"tier": "2", "degenerate": False},
    ])
    assert _trailing_degenerate_runs(st, "2") == 0


def test_counter_ignores_a_different_tier():
    st = make_state(FakeSpec(), accumulator=[
        {"tier": "2", "degenerate": True},
        {"tier": "1b", "degenerate": True},
    ])
    assert _trailing_degenerate_runs(st, "2") == 0


def test_counter_survives_junk_in_the_accumulator():
    st = make_state(FakeSpec(), accumulator=[{"tier": "2", "degenerate": True}, "not-a-dict"])
    assert _trailing_degenerate_runs(st, "2") == 0


# --------------------------------------------------------------- B: the circuit breaker
def test_breaker_stops_an_unbroken_degenerate_run(monkeypatch):
    """docs leg: no hop available, so the breaker is the only thing standing between the
    run and three more wasted rolls. Pinned to WORKSHOP_OFFLINE, where tier 2 tops the band --
    online, 15b0a84 gave tier 2 a cloud tier to escalate to (see the twin test below)."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.WORKSHOP_OFFLINE)
    spec = FakeSpec(tier="2", deliverable="docs")
    acc = [{"tier": "2", "attempt": 1, "degenerate": True}]
    out = escalate_subtask_node(make_state(spec, output=DEGEN, accumulator=acc))
    assert out.get("error"), "should stop, not re-roll"
    assert "degenerate" in out["error"]
    assert "spec" not in out, "must not schedule another attempt"


def test_first_degenerate_roll_still_gets_a_retry(monkeypatch):
    """N=2: one bad roll can be luck. Do not fail a leg on a single sample."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.WORKSHOP_OFFLINE)
    assert _MAX_CONSECUTIVE_DEGENERATE == 2
    spec = FakeSpec(tier="2", deliverable="docs")
    out = escalate_subtask_node(make_state(spec, output=DEGEN, accumulator=[]))
    assert out.get("error") is None
    assert out["spec"].assigned_tier == "2", "same-tier retry, as before"


def test_breaker_does_not_fire_on_ordinary_failure(monkeypatch):
    """A wrong-but-real answer must keep its full retry budget."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.WORKSHOP_OFFLINE)
    spec = FakeSpec(tier="2", deliverable="docs")
    acc = [{"tier": "2", "attempt": 1, "degenerate": False}]
    out = escalate_subtask_node(make_state(spec, output="a wrong but genuine answer",
                                           accumulator=acc))
    assert out.get("error") is None
    assert out["spec"].assigned_tier == "2"


def test_workshop_degenerate_tier_2_reaches_the_breaker_again(monkeypatch):
    """CEREMONY 118 (2026-10-13): WORKSHOP MODERATE is [1b, 2] again, so tier 2 is the band top
    as it is offline, and a degenerating tier 2 reaches the breaker, which exists for 'no tier
    left'. From 15b0a84 until 118 this leg went on to 3-lite-free instead."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.WORKSHOP)
    spec = FakeSpec(tier="2", deliverable="docs")
    acc = [{"tier": "2", "attempt": 1, "degenerate": True}]
    out = escalate_subtask_node(make_state(spec, output=DEGEN, accumulator=acc,
                                           tier_history=["1b", "2", "2"], attempt=2))
    assert "no escalation target remaining" in (out.get("error") or ""), out.get("error")
    assert out.get("spec") is None or out["spec"].assigned_tier != "3-lite-free"


def test_breaker_preserves_history_for_the_postmortem():
    spec = FakeSpec(tier="2", deliverable="docs")
    acc = [{"tier": "2", "attempt": 1, "degenerate": True}]
    out = escalate_subtask_node(make_state(spec, output=DEGEN, accumulator=acc))
    assert len(out["context_accumulator"]) == 2
    assert out["context_accumulator"][-1]["degenerate"] is True


def test_code_leg_hops_BEFORE_the_breaker_can_fire():
    """Ordering guarantee: a code leg with an escape must take it even mid-degenerate-run --
    the breaker is a last resort, not a competing rule."""
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    acc = [{"tier": "2", "attempt": 1, "degenerate": True}]
    out = escalate_subtask_node(make_state(spec, output=DEGEN, accumulator=acc))
    assert out.get("error") is None
    assert out["spec"].assigned_tier == "2.5"


def test_artifact_records_degeneracy():
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output=DEGEN))
    art = out["context_accumulator"][-1]
    assert art["degenerate"] is True
    assert art["partial_output"] == "", "noise must still be discarded, not replayed"


def test_artifact_flags_clean_output_as_not_degenerate():
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output="def f():\n    return 1\n"))
    assert out["context_accumulator"][-1]["degenerate"] is False

def test_jarvis_moderate_ends_on_the_cloud_leg():
    """The signed variant-A change (2026-08-10): JARVIS gets a TERMINAL decorrelation leg.

    JARVIS is deliberately EXCLUDED from the offline premise test above, not because the hop
    stopped mattering there -- see test_jarvis_code_leg_prefers_local_coder_over_cloud -- but
    because its band no longer ENDS on a non-coder local tier.
    """
    band = CASCADE_TABLES[OperatingMode.JARVIS][ComplexityBand.MODERATE]
    # 2026-09-10: 15b0a84 appended 3-lite-free, so T3_FAST is the first cloud leg, not the last.
    i = band.index(Tier.T2)
    assert band[i + 1] == Tier.T3_FAST, band
    assert all(t.value.startswith("3-") for t in band[i + 1:]), "only cloud tiers may follow tier 2"
    assert Tier.T2_5 not in band, "2.5 is still reached via the hop, not via the chain"


def test_the_bank_measurement_path_stays_pure_local():
    """The bank is pinned to JARVIS_OFFLINE precisely so the cloud leg cannot rescue it.

    If a cloud tier ever appears in an *_OFFLINE MODERATE band, the demo bank silently stops
    being a local-capability instrument -- and every conclusion drawn from it since v324 rests
    on it being one.
    """
    for mode in (OperatingMode.JARVIS_OFFLINE, OperatingMode.WORKSHOP_OFFLINE):
        band = CASCADE_TABLES[mode][ComplexityBand.MODERATE]
        assert not any(t.value.startswith("3-") for t in band), (mode, band)


def test_jarvis_code_leg_prefers_local_coder_over_cloud(monkeypatch):
    """Cloud is TERMINAL, not a queue-jumper.

    Appending T3_FAST gave `chain.index("2")+1` a value, so next_tier stopped being None and the
    chain-end hop silently stopped firing: a code leg went to Groq INSTEAD OF qwen3-coder:30b.
    Measured before the fix. Local strong coder first; cloud stays reachable after it.
    """
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS)
    spec = FakeSpec(tier="2", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output="def f(): pass"))
    assert out["spec"].assigned_tier == "2.5"


def test_jarvis_non_code_leg_still_gets_the_cloud_leg(monkeypatch):
    """A docs leg has no local coder to prefer, so the cloud tier is its escalation."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS)
    spec = FakeSpec(tier="2", deliverable="docs")
    out = escalate_subtask_node(make_state(spec, output="Some prose."))
    assert out["spec"].assigned_tier == "3-fast"


def test_cloud_is_still_reachable_after_the_local_coder_detour(monkeypatch):
    """Without the band-terminal fallback, preferring 2.5 would make cloud UNREACHABLE for a
    code leg -- defeating the Forge-outage case the cloud leg was approved for."""
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS)
    spec = FakeSpec(tier="2_qwen_coder", deliverable="code_selftest")
    acc = [{"tier": "1b", "degenerate": False}, {"tier": "2", "degenerate": False},
           {"tier": "2.5", "degenerate": False}]
    out = escalate_subtask_node(make_state(spec, output="def f(): pass", accumulator=acc,
                                           tier_history=["1b", "2", "2.5", "2_qwen_coder"]))
    # 3-fast, NOT 3-lite-free: the free overflow tier is last resort (_LAST_RESORT_TIERS).
    assert out["spec"].assigned_tier == "3-fast"


# CEREMONY 118 (2026-10-13) removed 3-lite-free from every MODERATE chain, and FRONTIER never ends
# on it, so the _LAST_RESORT_TIERS demotion changes no LIVE chain any more. It is kept as a guard
# against the tier being put back at a chain end, so it is proven on the chain it was written for.
_PRE_118_JARVIS_MODERATE = [Tier.T1B, Tier.T2, Tier.T3_FAST, Tier.T3_LITE_FREE]


def test_last_resort_tier_is_reached_once_3_fast_was_tried(monkeypatch):
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS)
    monkeypatch.setitem(CASCADE_TABLES[OperatingMode.JARVIS], ComplexityBand.MODERATE,
                        list(_PRE_118_JARVIS_MODERATE))
    spec = FakeSpec(tier="2_qwen_coder", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output="def f(): pass",
                                           tier_history=["1b", "2", "2.5", "3-fast", "2_qwen_coder"]))
    assert out["spec"].assigned_tier == "3-lite-free"


def test_last_resort_guard_still_demotes_a_chain_end_overflow_tier(monkeypatch):
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS)
    monkeypatch.setitem(CASCADE_TABLES[OperatingMode.JARVIS], ComplexityBand.MODERATE,
                        list(_PRE_118_JARVIS_MODERATE))
    spec = FakeSpec(tier="2_qwen_coder", deliverable="code_selftest")
    out = escalate_subtask_node(make_state(spec, output="def f(): pass",
                                           tier_history=["1b", "2", "2.5", "2_qwen_coder"]))
    assert out["spec"].assigned_tier == "3-fast"


def test_last_resort_tiers_match_fleet_config_overflow_role():
    import json
    from subtask_graph import _LAST_RESORT_TIERS
    fleet = json.loads((Path(__file__).resolve().parents[1] / "fleet_config.json").read_text(encoding="utf-8"))
    overflow = {t["tier"] for t in fleet["tiers"] if t.get("role") == "overflow"}
    assert set(_LAST_RESORT_TIERS) == overflow, (sorted(_LAST_RESORT_TIERS), sorted(overflow))


def test_band_terminal_fallback_never_escalates_DOWNWARD(monkeypatch):
    """My own bug, caught by probing rather than by reading: `reversed(chain)` picked the
    strongest UNTRIED tier, and on a fresh accumulator that included tiers BELOW the current
    one -- a docs leg on tier 2 in JARVIS_OFFLINE was sent to 1b. Escalation only moves up.
    """
    monkeypatch.setattr(settings, "current_mode", OperatingMode.JARVIS_OFFLINE)
    spec = FakeSpec(tier="2", deliverable="docs")
    out = escalate_subtask_node(make_state(spec, output="prose"))
    assert out["spec"].assigned_tier == "2", "nothing stronger exists -> same-tier retry"

