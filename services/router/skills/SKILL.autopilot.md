# SKILL: bifrost-autopilot

## What This Is
AUTOPILOT — autonomous parallel execution pipeline. Decomposes complex tasks into a DAG of subtasks, fans them out across the tier fleet with ThreadPoolExecutor, runs per-subtask verification harness, escalates failures up the tier chain, then assembles results.

## Key Files
| File | Purpose |
|------|---------|
| `autopilot_graph.py` | LangGraph AutopilotGraph — gate→decompose→fan-out→assemble |
| `subtask_graph.py` | LangGraph SubtaskGraph — execute→verify(L1-L4)→escalate→distill→loop |
| `bifrost_message.py` | SubtaskSpec + SubtaskResult schemas |

## Pipeline Stages
```
Request → DecompositionGate (token_count>40, keyword_matches≥2)
        → Decomposer (T2_5 / 72B) → DAG of SubtaskSpecs
        → Wave-based fan-out (ThreadPoolExecutor, wave = parallel subtasks)
        → Per-subtask: SubtaskGraph loop (MAX_ATTEMPTS=3)
            → execute (tier assignment)
            → verify (L1 syntax / L2 schema / L3 ruff+mypy / L4 semantic cosine)
            → escalate (1a→1a-hearth→1b on failure)
            → distill (1a-overflow compresses context for next tier)
        → Assemble (combine completed subtasks)
```

## Verification Harness Levels
| Level | Check | Status |
|-------|-------|--------|
| L1 | Syntax/parse | Live |
| L2 | Schema validation | Live |
| L3 | ruff + mypy via bifrost-shell MCP | Live |
| L4 | Semantic cosine (nomic-embed-text) | Live |
| L5 | Unit test runner | Pending (Session C) |
| L6 | Cross-subtask integration | Pending (Session C) |

## Trigger
- `POST /v1/chat/completions` with `X-Strategy: autopilot` header OR `"strategy": "AUTOPILOT"` in body
- Portal Converse surface (primary UI)

## Context Budgets (per tier)
1a=4K, 1b=8K, T2=16K, T2.5=32K, Cloud=64K+

## Loop Budget
MAX_ATTEMPTS=3 per subtask. Wall-clock timeout enforced. Cloud spend caps: $0.50/subtask, $5.00/graph, $20.00/day.

## Key Constraints
- nest_asyncio removed — uses `loop.run_until_complete()` for sync wrapper around async nodes
- Decomposer uses Instructor structured parsing with DecompositionResult Pydantic model + json.loads fallback
- Fan-out: multi-agent coordination degrades above ~45% single-agent baseline — DecompositionGate prevents over-decomposition
