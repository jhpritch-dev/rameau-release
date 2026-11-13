# BIFROST Cascade Tables Reference

## Fleet IPs and Ports

| Machine | IP | Key Ports |
|---------|-----|-----------|
| Bifrost | 192.168.x.33 | Router:8080, Arbiter:8082, Ollama-GPU:11434, Ollama-CPU:11435, anthropic-adapter:8085 |
| Hearth  | 192.168.x.4  | Portal:3110, Broadcaster:8092, Observer:8081, bifrost-kb:8091, Ollama:11434, Vega8:11436 |
| Forge   | 192.168.x.50 | Ollama:11434, Lemonade-NPU:8000 |

## Tier → Backend Map

| Tier | Model | Host |
|------|-------|------|
| T1A_CODER | qwen2.5-coder:7b | Bifrost :11434 |
| T1A_HEARTH | qwen2.5-coder:7b-ctx4k | Hearth :11434 |
| T1A_OVERFLOW | qwen3.5:4b | Hearth :11436 (Vega8) |
| T1A_INSTRUCT | qwen2.5:7b-instruct | Bifrost :11434 |
| T1B | bifrost-interactive | Bifrost :11434 |
| T2 | qwen2.5-coder:14b | Forge :11434 |
| T2_5 | qwen2.5:72b | Forge :11434 |
| T3_CLAUDE | claude-sonnet-4-6 | Anthropic API |
| T3_GEMINI | gemini-2.5-flash | Gemini API |
| T3_FAST | llama-3.3-70b-versatile | Groq API |
| T_NPU | Qwen3-1.7b-FLM | Forge Lemonade :8000 |

## CASCADE_TABLES

### JARVIS (full fleet, cloud allowed)
| Band | Cascade |
|------|---------|
| TRIVIAL | NPU → 1a-hearth → 1a-overflow → 1a-coder |
| MODERATE | 1a-hearth → 1a-overflow → 1a-coder → 1b → T2 |
| COMPLEX | T2 → T2_5 → T3_CLAUDE |
| FRONTIER | T3_CLAUDE |

### JARVIS_OFFLINE (full fleet, zero cloud egress)
| Band | Cascade |
|------|---------|
| TRIVIAL | NPU → 1a-hearth → 1a-overflow → 1a-coder |
| MODERATE | 1a-hearth → 1a-overflow → 1a-coder → 1b → T2 |
| COMPLEX | T2 → T2_5 |
| FRONTIER | T2_5 (tops here — no cloud escalation) |

### WORKSHOP (Bifrost+Hearth, Forge offline)
| Band | Cascade |
|------|---------|
| TRIVIAL | 1a-hearth → 1a-overflow → 1a-coder |
| MODERATE | 1a-hearth → 1a-overflow → 1a-coder → 1b → T3_FAST |
| COMPLEX | T3_GEMINI |
| FRONTIER | T3_CLAUDE |

### WORKSHOP_OFFLINE (Bifrost+Hearth, zero cloud)
| Band | Cascade |
|------|---------|
| TRIVIAL | 1a-hearth → 1a-overflow → 1a-coder |
| MODERATE | 1a-hearth → 1a-overflow → 1a-coder |
| COMPLEX | 1a-overflow → 1a-coder |
| FRONTIER | 1a-coder |

### CLOUD_PLUS / CLOUD_ONLY
See config.py — cloud-first, rarely used.

## Model Family Rules
- **Qwen3**: Bifrost only (RDNA4 coopmat required — Forge 8060S fails)
- **Qwen2.5**: Forge + Hearth (proven)
- **Qwen3.5**: Hearth validated (4b=40 tok/s on 5700XT, 0.8b=84 tok/s Vega8)
- **30 tok/s floor** for interactive use — below this = background/AUTOPILOT only
