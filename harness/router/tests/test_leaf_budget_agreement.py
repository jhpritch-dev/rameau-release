"""Pin the leaf context budget to ONE value per tier (2026-10-13).

A leaf leg sends context_budget_tokens as options.num_ctx, and a caller num_ctx always wins in
backends/ollama.py, so a budget that disagrees with the tier's fleet_config num_ctx reloads the
model at that size. escalate_subtask_node used to re-read a drifted table on every retry and
escalation (1b 8192, 1a-hearth 4096, 1a-coder 8192, 2.5 32768). These tests hold fleet_config,
subtask_graph._CONTEXT_BUDGETS and autopilot_graph._LEAF_CONTEXT_BUDGETS together, and drive the
real node so a call site cannot slip back to the old table.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TIER_BACKENDS  # noqa: E402
import subtask_graph as SG  # noqa: E402
from autopilot_graph import _LEAF_CONTEXT_BUDGETS  # noqa: E402
from test_degenerate_escalation import FakeSpec, make_state  # noqa: E402

LEAVES = sorted(SG._LEAF_TIERS)


@pytest.mark.parametrize("tier", LEAVES)
def test_leaf_budget_equals_fleet_num_ctx_unless_capped(tier):
    n = (TIER_BACKENDS.get(tier) or {}).get("num_ctx")
    if tier in SG._LEAF_BUDGET_CAPS:
        cap = SG._LEAF_BUDGET_CAPS[tier]
        assert SG.context_budget_for_tier(tier) == (min(cap, n) if n else cap)
    else:
        assert n, "leaf tier %s has no fleet_config num_ctx" % tier
        assert SG.context_budget_for_tier(tier) == n


@pytest.mark.parametrize("tier", LEAVES)
def test_static_table_does_not_contradict_the_leaf_budget(tier):
    if tier in SG._CONTEXT_BUDGETS:
        assert SG._CONTEXT_BUDGETS[tier] == SG.context_budget_for_tier(tier)


@pytest.mark.parametrize("tier", sorted(_LEAF_CONTEXT_BUDGETS))
def test_autopilot_assignment_agrees_with_retry_and_escalation(tier):
    assert _LEAF_CONTEXT_BUDGETS[tier] == SG.context_budget_for_tier(tier)


@pytest.mark.parametrize("tier", LEAVES)
def test_every_leaf_tier_has_an_autopilot_budget(tier):
    # Agreement "per shared key" let 2 and 2_devstral fall to _DEFAULT_CONTEXT_BUDGET (8192) on
    # their first attempt. A leaf with no entry is the same defect, so it is a failure here.
    assert tier in _LEAF_CONTEXT_BUDGETS, "leaf tier %s has no autopilot _LEAF_CONTEXT_BUDGETS entry" % tier
    assert _LEAF_CONTEXT_BUDGETS[tier] == SG.context_budget_for_tier(tier)


def test_measured_values_2026_10_13():
    # The four that were wrong, at the values the fleet runs today.
    assert SG.context_budget_for_tier("1b") == 16384
    assert SG.context_budget_for_tier("1a-coder") == 16384
    assert SG.context_budget_for_tier("1a-hearth") == 32768
    # 2.5: John 2026-10-13 "fix the 2.5 leg cap to 65536" -- the ctx gpt-oss-igor-65k runs at.
    assert SG.context_budget_for_tier("2.5") == 65536


class _FakeTB:
    def __init__(self, num_ctx):
        self.num_ctx = num_ctx

    def get(self, tier, *_a):
        return {"num_ctx": self.num_ctx} if tier == "2.5" else {}


@pytest.mark.parametrize("fleet_ctx,want", [(262144, 65536), (65536, 65536), (32768, 32768)])
def test_the_25_cap_is_a_maximum_not_a_value(monkeypatch, fleet_ctx, want):
    # R8 (2026-07-30): with qwen3-coder at 262144 on 2.5, uncapped legs OOM-killed llama-server on
    # Forge. The cap must still hold against a larger-ctx model, and never ask for MORE ctx than a
    # smaller one runs at (that would reload it too).
    monkeypatch.setattr(SG, "TIER_BACKENDS", _FakeTB(fleet_ctx))
    assert SG.context_budget_for_tier("2.5") == want


def test_non_leaf_keeps_the_advisory_table():
    assert SG.context_budget_for_tier("3-claude") == SG._CONTEXT_BUDGETS["3-claude"]
    assert SG.context_budget_for_tier("no-such-tier") == 8192
    assert SG.context_budget_for_tier("no-such-tier", 1234) == 1234


def test_unreadable_fleet_config_falls_back_to_the_table(monkeypatch):
    class Boom:
        def get(self, *_a, **_k):
            raise OSError("fleet_config.json unreadable")
    monkeypatch.setattr(SG, "TIER_BACKENDS", Boom())
    assert SG.context_budget_for_tier("1b") == SG._CONTEXT_BUDGETS["1b"]


def _escalated_budget(spec, tier_history):
    out = SG.escalate_subtask_node(make_state(spec, output="wrong", attempt=1, tier_history=tier_history))
    return out.get("spec")


def test_escalation_onto_a_leaf_carries_that_leafs_budget():
    chain = SG.get_cascade("MODERATE")
    pairs = [(a, b) for a, b in zip(chain, chain[1:]) if b in SG._LEAF_TIERS and b not in SG._LEAF_BUDGET_CAPS]
    assert pairs, "no leaf follows another tier in MODERATE: %s" % chain
    cur, nxt = pairs[0]
    spec = FakeSpec(tier=cur, deliverable="docs")
    spec.context_budget_tokens = 1111  # a stale value the node must not carry or re-derive wrongly
    new = _escalated_budget(spec, [cur])
    assert new is not None and new.assigned_tier == nxt
    assert new.context_budget_tokens == (TIER_BACKENDS.get(nxt) or {}).get("num_ctx")


_TRIED_EVERYWHERE = ["1b", "2", "3-lite-free", "2.5", "2_qwen_coder", "2_devstral", "1a-hearth", "1a-coder"]


# Measured 2026-10-13: with every tier already tried, these two leaves take the same-tier
# re-roll branch. Before the fix they re-rolled at 4096 and 32768.
@pytest.mark.parametrize("tier,want", [("1a-hearth", 32768), ("2.5", 65536)])
def test_same_tier_retry_carries_the_leafs_budget(tier, want):
    spec = FakeSpec(tier=tier, deliverable="docs")
    spec.context_budget_tokens = 1111
    out = SG.escalate_subtask_node(make_state(spec, output="wrong", attempt=1, tier_history=_TRIED_EVERYWHERE))
    new = out.get("spec")
    assert new is not None and new.assigned_tier == tier, "branch not reached: %r" % (out.get("error"),)
    assert new.context_budget_tokens == want == SG.context_budget_for_tier(tier)


# ---- the two hard-coded autopilot budgets (2026-10-13): direct_route_node and _build_rfp_dag ----
import autopilot_graph as AG  # noqa: E402


def test_direct_route_leg_carries_the_1b_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(AG, "run_subtask", lambda spec: seen.append(spec) or None)
    out = AG.direct_route_node({"graph_id": "g-budget", "original_prompt": "say ok"})
    assert seen, "direct_route_node did not reach run_subtask: %r" % (out,)
    assert seen[0].assigned_tier == "1b"
    assert seen[0].context_budget_tokens == SG.context_budget_for_tier("1b") == 16384


def _specs_in(obj):
    if isinstance(obj, SG.SubtaskSpec):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _specs_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _specs_in(v)


def test_rfp_sections_carry_their_tiers_budget():
    out = AG._build_rfp_dag({"graph_id": "g-rfp", "original_prompt": "write an RFP"})
    sections = [s for s in _specs_in(out) if s.subtask_id != "s6"]
    assert len(sections) >= 4, "expected the RFP section specs, got %d" % len(sections)
    leaf = [s for s in sections if s.assigned_tier in SG._LEAF_TIERS]
    assert leaf, "no RFP section runs on a leaf tier: %s" % [s.assigned_tier for s in sections]
    for s in sections:
        want = (SG.context_budget_for_tier(s.assigned_tier) if s.assigned_tier in SG._LEAF_TIERS
                else AG._DEFAULT_CONTEXT_BUDGET)
        assert s.context_budget_tokens == want, (s.subtask_id, s.assigned_tier, s.context_budget_tokens, want)
