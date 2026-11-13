# SKILL: bifrost-router

## What This Is
BIFROST Complexity Router — FastAPI service on Bifrost:8080. Classifies incoming inference requests by complexity band (TRIVIAL/MODERATE/COMPLEX/FRONTIER) and routes to the appropriate local or cloud tier via cascade tables.

## Key Files
| File | Purpose |
|------|---------|
| `main.py` | FastAPI app, `/v1/chat/completions` handler, INTERACTIVE + AUTOPILOT strategy branching |
| `config.py` | Tier enums, TIER_BACKENDS, CASCADE_TABLES, ClassifierConfig, RouterSettings |
| `router_graph.py` | LangGraph RouterGraph — classify→assign_tier→execute nodes |
| `backends/ollama.py` | Ollama OpenAI-compat proxy, options passthrough, streaming relay |
| `backends/anthropic.py` | Anthropic API backend |
| `openai_compat.py` | Gemini + Groq + Lemonade (NPU) OpenAI-compat adapters |
| `bifrost_message.py` | Pydantic schemas — BifrostMessage, RouterRequest, SubtaskSpec |
| `classifier.py` | Rule-based complexity classifier (keyword + token count) |
| `telemetry.py` | InferenceEvent schema + async SQLite writes |

## Ports & Endpoints
- Router: `http://localhost:8080` (Bifrost)
- Key endpoints: `POST /v1/chat/completions`, `POST /classify`, `POST /strategy`, `GET /status`, `GET /metrics`

## Tier Map
| Tier | Model | Machine | Port |
|------|-------|---------|------|
| T_NPU | Qwen3-1.7b-FLM | Forge Lemonade | :8000 |
| T1A_HEARTH | qwen2.5-coder:7b | Hearth 5700XT | :11434 |
| T1A_OVERFLOW | bifrost-1a-overflow | Hearth Vega8 | :11436 |
| T1A_CODER | bifrost-1a-coder | Bifrost 9070XT | :11434 |
| T1A_INSTRUCT | bifrost-1a-instruct | Bifrost 9070XT | :11434 |
| T1B | bifrost-interactive | Bifrost 9070XT | :11434 |
| T2 | bifrost-t2 | Forge 8060S | :11434 |
| T2_5 | bifrost-t2p5 | Forge 8060S | :11434 |
| T3_CLAUDE | claude-sonnet-4-6 | Anthropic API | — |
| T3_GEMINI | gemini-2.5-flash | Gemini API | — |
| T3_FAST | llama-3.3-70b | Groq API | — |

## Patch Delivery Pattern
- Always use Python patch scripts (`fix_*.py`) — PowerShell heredocs fail silently on multiline strings
- Read with `encoding="utf-8", newline=""` then `.replace("\r\n", "\n")` before string matching
- Write back with `encoding="utf-8"` (no newline param — Python handles it)

## Post-Processing (INTERACTIVE non-streaming)
- T1A tiers: code fence stripping (regex, `main.py` ~line 510)
- T1B: `think` option passed via Ollama options (`False` for MODERATE, `True` for COMPLEX)
- T1A_OVERFLOW: `think=False` always

## Running
```powershell
# [Bifrost]
cd D:\Projects\bifrost-router
python main.py
```
