"""
autopilot_state.py -- BIFROST AutopilotGraph state types (Phase 3b)
"""

from typing import Annotated, Literal, TypedDict

from bifrost_message import SubtaskResult, SubtaskSpec


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def _merge_dict(a: dict, b: dict) -> dict:
    """Merge dicts -- b wins on conflict. Used for subtask result accumulation."""
    return {**a, **b}


def _add_float(a: float, b: float) -> float:
    return a + b


# ---------------------------------------------------------------------------
# Subtask sub-graph state -- one instance per subtask
# ---------------------------------------------------------------------------

class SubtaskState(TypedDict):
    spec:               SubtaskSpec
    result:             SubtaskResult | None
    attempt_count:      int
    tier_history:       list[str]          # tiers tried in order
    context_accumulator: list[dict]        # escalation artifacts per attempt
    verification_level: int                # highest level passed
    error:              str | None


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------
# Declared BEFORE AutopilotState on purpose: this module has no
# `from __future__ import annotations`, so TypedDict annotations are evaluated at class
# creation. AutopilotState._gate references GateDecision, and a forward reference would
# NameError at import time.

class GateDecision(TypedDict):
    route:          Literal["decompose", "direct_route"]
    reason:         str
    tool_density:   float
    seq_depth:      float
    # UNUSED (2026-07-16 audit). Set to 0 at every gate_node site except a vestigial 6 for
    # RFP, and read by nobody. The RFP 6 is not produced here either: RFP bypasses the AI
    # decomposer entirely (autopilot_graph.decompose_node -> _build_rfp_dag, a
    # deterministic 5 sections + 1 coherence pass), so this field has never influenced
    # anything. Kept as the ready-made slot IF a host-side leg recommender is ever wanted
    # -- gate_node already computes token_count / tool_density / multi_step / matched for
    # free. Deliberately NOT wired: leg count is pinned by the caller instead
    # (`legs` on /autopilot/submit -> leg_target -> the decoding grammar), because a
    # keyword heuristic guessing N is a guess and the grammar is a guarantee.
    subtask_count:  int


# ---------------------------------------------------------------------------
# AutopilotGraph state -- top-level graph
# ---------------------------------------------------------------------------

class AutopilotState(TypedDict):
    # Identity
    graph_id:   str
    trace_id:   str

    # Input
    original_prompt:    str
    original_messages:  list[dict]
    warp_seed:          dict | None
    budget_usd:         float | None
    preserve_thinking:  bool          # K+1: gate cold-persistence of subtask reasoning to decisions.md
    leg_target:         int | None    # pin the decomposition to exactly N legs; None = default (>=2)
    profile:            str | None    # research | orchestrator | coding | general | None; only "research" evicts the decomposer (John 2026-09-11)

    # Routing
    # gate_node's decision. It ALWAYS returned this key, but the field was absent from this
    # TypedDict, so LangGraph silently DROPPED it and the whole GateDecision -- route,
    # reason, subtask_count -- was discarded the moment the gate returned. Verified against
    # a live state dump: no _gate among the surviving keys. That is why nothing read it: it
    # was unreachable, not merely unwired. Single writer (gate_node, runs once, before
    # everything), so plain last-write-wins -- NO Annotated[] reducer. None until the gate
    # runs.
    _gate:              GateDecision | None

    # Decomposition
    subtasks:       Annotated[dict[str, SubtaskSpec],   _merge_dict]
    dag_edges:      dict[str, list[str]]   # subtask_id -> list of dependent subtask_ids

    # Execution results
    completed:      Annotated[dict[str, SubtaskResult], _merge_dict]
    failed:         Annotated[dict[str, str],           _merge_dict]  # id -> error

    # Cost tracking
    cloud_cost_usd: Annotated[float, _add_float]

    # Output
    assembled_output:       str | None
    status:                 Literal[
        "DECOMPOSING",
        "EXECUTING",
        "ASSEMBLING",
        "COMPLETE",
        # PARTIAL: finished, but subtasks failed (or the assembler fell back). Emitted by
        # _assembly_status since v220 and MISSING here until now -- the annotation lied.
        # Runtime was unaffected (TypedDict Literals are hints, not enforcement, which is
        # why PARTIAL worked live), but a reader trusting this list would conclude the
        # router cannot emit it. The Portal's terminal-status list is the OTHER half of
        # this contract; keep the two in step.
        "PARTIAL",
        "FAILED",
        "DIRECT_ROUTE",
        "FAILED_NEEDS_HUMAN",
    ]
    error:                  str | None
    human_review_required:  bool

    # 2026-09-07 (DB audit): _write_decisions_log reads state["_started_at"] to emit
    # **Wall time**, and it emitted on 0 of 530 entries -- because this field was never
    # declared here and LangGraph DROPS undeclared keys. That is the same failure the
    # _gate comment above records. Declaring it is only half the fix; run_autopilot must
    # also set it. Single writer, set once at graph entry, so plain last-write-wins --
    # NO Annotated[] reducer.
    _started_at:            float | None
