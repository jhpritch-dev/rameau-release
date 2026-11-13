# SKILL: Router
## BIFROST Complexity Router — D:\Projects\bifrost-router\

Entry point: main.py (FastAPI, :8080)
Core graph: router_graph.py (LangGraph)
Config: config.py (tiers, cascade tables, classifier)
Compat layer: openai_compat.py (provider normalization)

Key patterns: cascade walk, complexity classification, static analysis post-processing (ruff on INTERACTIVE output).
