# SKILL: AUTOPILOT
## LangGraph multi-agent pipeline — D:\Projects\bifrost-router\

Graphs: autopilot_graph.py (decompose→fan-out→assemble), subtask_graph.py (execute→verify→escalate→distill→loop)
Decomposer: Forge T2_5 (qwen2.5:72b), Instructor structured parsing, DecompositionResult Pydantic model
Fan-out: wave-based DAG, ThreadPoolExecutor
Verification harness: L1 non-empty → L2 keyword → L3 ruff/mypy (bifrost-shell) → L4 cosine similarity
MAX_ATTEMPTS=3 per subtask. SQLite checkpointer + MemorySaver fallback.
