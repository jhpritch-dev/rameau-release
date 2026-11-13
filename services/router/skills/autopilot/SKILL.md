---
name: bifrost-autopilot
description: AUTOPILOT multi-agent task graph execution for the BIFROST platform. Use when user types /autopilot, asks to build a multi-file feature, describes a task with 3+ discrete steps, or any construction task that benefits from parallel subtask execution. Don't wait for explicit /autopilot invocation — if the task clearly involves decomposition, subtask routing, the Decomposer/Assembler/Verification Harness, or autopilot_graph.py, use this skill.
---

# BIFROST AUTOPILOT Skill

AUTOPILOT decomposes complex tasks into a DAG of subtasks, routes each to the optimal tier, verifies outputs at multiple levels, and assembles a final result.

## Key files
- `autopilot_graph.py` — top-level LangGraph graph (Decomposer → Scheduler → SubtaskGraph fan-out → Assembler)
- `subtask_graph.py` — single subtask lifecycle: execute → verify → escalate → distill → loop
- `bifrost_message.py` — SubtaskSpec, SubtaskResult, TaskGraph Pydantic models

## Architecture
```
Request → Decomposer (T2_5, Instructor+Pydantic) → TaskGraph (DAG)
        → Tier Scheduler (mode-aware placement, semaphore contention)
        → SubtaskGraph × N (parallel where no dependency edge)
            execute → verify (L1-L6) → escalate → distill → loop
        → Assembler → decisions.md entry → final output
```

## Verification levels
- L1: non-empty output
- L2: no refusal/error message
- L3: ruff + mypy clean (via bifrost-shell MCP, ~100ms)
- L4: semantic similarity vs acceptance criteria (cosine, Hearth Vega8 embed)
- L5: pytest unit test runner (tempfile → subprocess → parse pass/fail)
- L6: cross-subtask integration check (import smoke test on assembled output)

## Grading output schema (L5/L6)
```json
{"text": "assertion description", "passed": true, "evidence": "specific proof string"}
```
Field names are exact — `text`, `passed`, `evidence`. No variants.

## Loop budget and caps
- MAX_ATTEMPTS = 3 per subtask
- Wall-clock timeout: 300s per subtask
- Cloud spend caps: $0.50/subtask, $5.00/graph, $20.00/day
- Per-tier context budgets: 1a=4K, 1b=8K, 2=16K, 2.5=32K, cloud=64K+

## Mode-aware placement
In JARVIS_OFFLINE: Scheduler must refuse cloud tiers at assignment time.
In JARVIS: cloud allowed as last escalation step only.

## decisions.md drop-box
Assembler appends a structured entry to `decisions.md` in project root:
- tier chosen per subtask, why, outcome
- enables re-entry after restart + demo legibility

## Common tasks

### Add a verification level
Add `_verify_level_N()` in `subtask_graph.py`, call from `verify_subtask_node`, update `verification_level` in state.

### Tune decomposition
Decomposer uses Instructor with `response_model=TaskGraph, max_retries=3`. Edit the system prompt in `autopilot_graph.py` Decomposer node.

### Check Decomposition Gate (§7.6)
Before fan-out: tool density check + capability saturation + sequential dependency depth.
Multi-agent yields negative returns above ~45% single-agent baseline — gate prevents over-decomposition.
