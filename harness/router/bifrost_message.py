"""
bifrost_message.py -- BIFROST message envelope and payload schemas
Phase 3a: RouterRequest only (INTERACTIVE strategy)
Phase 3b: SubtaskSpec + SubtaskResult (AUTOPILOT strategy)
"""

import hashlib
import time
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Phase 3a -- Router payload
# ---------------------------------------------------------------------------

class RouterRequest(BaseModel):
    type: Literal["router_request"]
    prompt: str
    messages: list[dict]
    complexity_hint: str | None = None
    strategy: Literal["INTERACTIVE", "AUTOPILOT", "WARP"] = "INTERACTIVE"
    stream: bool = False
    budget_usd: float | None = None
    warp_seed: dict | None = None


# ---------------------------------------------------------------------------
# Phase 3b -- Autopilot payloads
# ---------------------------------------------------------------------------

class SubtaskSpec(BaseModel):
    type: Literal["subtask_spec"]
    graph_id: str
    subtask_id: str
    description: str
    acceptance_criteria: list[str]
    assigned_tier: str
    complexity_band: str = "MODERATE"  # TRIVIAL/MODERATE/COMPLEX/FRONTIER
    dependencies: list[str] = []
    context_budget_tokens: int = 8192
    attempt_number: int = 0
    max_attempts: int = 3
    think: bool = False
    partial_output: str | None = None
    known_dead_ends: list[str] = []
    # What KIND of artifact this leg produces: "code" | "tests" | "docs".
    #
    # 2026-07-23. Before this, every consumer re-derived the kind from free text with its
    # own regex -- _CODE_TASK_RE and _is_test_leg in subtask_graph, _is_test_leg and
    # _is_impl_leg in autopilot_graph -- and they DISAGREED. Measured the same day: two s4
    # legs with the same job were gated differently because of how the decomposer happened
    # to phrase them. One passed while its sandbox verdict said passed=False (the wording
    # missed _CODE_TASK_RE, so L5 was skipped and the failure never counted); two others
    # failed their takes on the identical error. A verification gate that fires on prose
    # phrasing is a coin flip in both directions -- it lets bad work through as readily as
    # it fails good work.
    #
    # Resolved ONCE, host-side, in decompose_node (_resolve_deliverable) and carried here.
    # None means "not resolved" -- only for a graph decomposed by an older build; consumers
    # fall back to their legacy regex in that case and must keep doing so.
    deliverable: str | None = None
    # Interface contracts (P4A) -- wired by Decomposer, used by Assembler
    function_name: str | None = None       # e.g. "parse_csv"
    input_signature: str | None = None     # e.g. "file_path: str"
    output_signature: str | None = None    # e.g. "pd.DataFrame"
    # Outputs of this subtask's `dependencies`, collected and budgeted by
    # autopilot_graph.fan_out_node (_with_dep_context) and injected by subtask_graph.
    # None when the subtask declares no dependencies, which is the common case.
    #
    # Deliberately NOT folded into `description`: the stub detector reads
    # `spec.description` to decide _is_code_task, and L3 keyword-coverage scores against
    # it, so pasting an upstream implementation in there would corrupt verification.
    #
    # Before this existed, `dependencies` only gated SCHEDULING ORDER -- a dependent leg
    # ran later but was just as blind, so a unit-test leg could not see the function it
    # was testing and honestly refused ("I do not have the information...").
    dep_context: str | None = None
    # s0 contract-leg prototype (2026-07-27): the subtask_id of the CONTRACT leg (a stub that
    # pins the shared API) this leg binds to. Set on the impl leg (it must DEFINE every contract
    # symbol) and the test leg (it authors against the contract). None for every non-contract
    # decomposition, so the standard path is unchanged. _with_dep_context reads it to mark the
    # contract section, and _l5_from_sandbox reads that marker for the conformance gate.
    contract_dep: str | None = None
    # Executable acceptance checker (2026-07-29), authored by the decomposer (Igor) in a dedicated
    # post-decompose call and attached to the impl + test legs of a pair. Shape:
    #   {"entrypoint": "main", "fixtures": {name: bytes_str},
    #    "invocations": [{"args": [...]}, ...],
    #    "checker": "<python source defining def check(stdout, stderr, rc, args) -> (ok, reason)>",
    #    "self_validation": {"good": {...}, "bad": {...}}}
    # None for every decomposition without BIFROST_ACCEPT_CHECKER or when authoring/validation fails
    # (fallback = current behavior), so the standard path is unchanged. The s1 gate synthesizes a
    # self-validating acceptance harness from this; a broken/non-discriminating checker is skipped,
    # never a failure. See DESIGN-s4-executable-checker-2026-07-29.
    acceptance: dict | None = None


class SubtaskResult(BaseModel):
    type: Literal["subtask_result"]
    subtask_id: str
    graph_id: str
    status: Literal["PASS", "FAIL", "ESCALATE", "FAILED_NEEDS_HUMAN"]
    output: str
    output_checksum: str
    verification_level_reached: int
    tier_used: str
    tokens_used: int
    cloud_cost_usd: float = 0.0
    escalation_artifact: dict | None = None
    tool_loop: dict | None = None   # per-leg run_tool_loop trajectory: {tools_called, successful_tool_calls, iterations, reason}


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

Payload = Annotated[
    RouterRequest | SubtaskSpec | SubtaskResult,
    Field(discriminator="type"),
]


class BifrostMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str
    created_at: float = Field(default_factory=time.time)
    source: str
    payload: Payload


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def compute_checksum(text: str) -> str:
    """SHA-256 checksum for subtask output integrity verification."""
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Round-trip smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    trace = str(uuid.uuid4())

    # RouterRequest round-trip
    rr = RouterRequest(
        type="router_request",
        prompt="Implement a binary search function",
        messages=[{"role": "user", "content": "Implement a binary search function"}],
        complexity_hint="moderate",
        strategy="INTERACTIVE",
    )
    msg1 = BifrostMessage(trace_id=trace, source="continue", payload=rr)
    json1 = msg1.model_dump_json()
    recovered1 = BifrostMessage.model_validate_json(json1)
    assert recovered1.payload.type == "router_request"
    print(f"RouterRequest:  OK  checksum={compute_checksum(json1)[:16]}...")

    # SubtaskSpec round-trip
    ss = SubtaskSpec(
        type="subtask_spec",
        graph_id="graph-001",
        subtask_id="subtask-001",
        description="Implement binary search core logic",
        acceptance_criteria=["handles empty list", "returns correct index", "handles duplicates"],
        assigned_tier="1b",
        dependencies=[],
        context_budget_tokens=8192,
        think=False,
    )
    msg2 = BifrostMessage(trace_id=trace, source="decomposer", payload=ss)
    json2 = msg2.model_dump_json()
    recovered2 = BifrostMessage.model_validate_json(json2)
    assert recovered2.payload.type == "subtask_spec"
    print(f"SubtaskSpec:    OK  checksum={compute_checksum(json2)[:16]}...")

    # SubtaskResult round-trip
    output = "def binary_search(arr, target): ..."
    sr = SubtaskResult(
        type="subtask_result",
        subtask_id="subtask-001",
        graph_id="graph-001",
        status="PASS",
        output=output,
        output_checksum=compute_checksum(output),
        verification_level_reached=3,
        tier_used="1b",
        tokens_used=312,
    )
    msg3 = BifrostMessage(trace_id=trace, source="executor", payload=sr)
    json3 = msg3.model_dump_json()
    recovered3 = BifrostMessage.model_validate_json(json3)
    assert recovered3.payload.type == "subtask_result"
    assert recovered3.payload.output_checksum == compute_checksum(output)
    print(f"SubtaskResult:  OK  checksum={compute_checksum(json3)[:16]}...")

    print("\nAll round-trips passed.")
