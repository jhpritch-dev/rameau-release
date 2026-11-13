# Cascade tables and tier map

**`config.py` beside this file is the authority. This document is a reading of it, and it was
wrong.** Corrected 2026-11-13 against the `config.py` that actually ships in this image; the
section at the bottom records what it said before, because a claim a reader can find in the
history and believe again is worse than one that says what replaced it.

Every value below was read out of `config.py` rather than recalled. If the two ever disagree
again, `config.py` wins and this file is the defect.

## Endpoints

This image resolves every endpoint from the environment through `shared/resolver.py`, which
**raises rather than defaulting** when a variable is unset. There is no address to look up here
and no built-in host:

| Endpoint | Variable | Needed for |
|---|---|---|
| ollama | `RAMEAU_OLLAMA_URL` | core — every local tier |
| embed | `RAMEAU_EMBED_URL` | embeddings, including the routing cache |
| arbiter | `RAMEAU_ARBITER_URL` | arbitration |
| kb | `RAMEAU_KB_URL` | retrieval |
| broadcaster | `RAMEAU_BROADCASTER_URL` | observability |
| search | `RAMEAU_SEARCH_URL` | search |
| portal | `RAMEAU_PORTAL_URL` | portal |

`shared/resolver.py` carries the full registry, the compatibility aliases, and the example
value for each.

## Tier → backend map

The left column is the **enum value**, which is what `X-Tier-Override` accepts. These are not
the same strings this file used to print; see the correction note.

| Tier | Backend | Model |
|---|---|---|
| `1a-coder` | ollama | qwen2.5-coder:7b |
| `1a-hearth` | ollama | qwen2.5-coder:7b-ctx4k |
| `1a-instruct` | ollama | qwen2.5:7b-instruct |
| `1b` | ollama | qwen2.5-coder:14b |
| `2` | ollama | qwen2.5-coder:32b |
| `2.5` | ollama | qwen2.5:72b |
| `3-claude` | anthropic | claude-sonnet-4-5-20250929 |
| `3-gemini` | openai_compat | gemini-2.5-flash |
| `3-fast` | openai_compat | llama-3.3-70b-versatile |

`1a-hearth` keeps a name from the deployment this was derived from. It is a tier identifier,
not a host: which machine serves it is decided by `RAMEAU_OLLAMA_URL`, like every other
ollama-backed tier.

## Bands

Four, and only four — `TRIVIAL`, `MODERATE`, `COMPLEX`, `FRONTIER`. Every operating mode below
defines a cascade for all four, so no band can fall off the end of a table.

## CASCADE_TABLES

Five operating modes. A cascade is walked left to right and the first available tier answers.

### JARVIS
| Band | Cascade |
|---|---|
| TRIVIAL | `1a-hearth` → `1a-coder` |
| MODERATE | `1a-hearth` → `1a-coder` → `1b` → `2` |
| COMPLEX | `2` → `2.5` → `3-claude` |
| FRONTIER | `3-claude` |

### WORKSHOP
| Band | Cascade |
|---|---|
| TRIVIAL | `1a-hearth` → `1a-coder` |
| MODERATE | `1a-hearth` → `1a-coder` → `1b` → `3-fast` |
| COMPLEX | `3-gemini` |
| FRONTIER | `3-claude` |

### WORKSHOP_OFFLINE
Zero cloud egress — every tier here is local.

| Band | Cascade |
|---|---|
| TRIVIAL | `1a-hearth` → `1a-coder` |
| MODERATE | `1a-hearth` → `1a-coder` |
| COMPLEX | `1a-coder` |
| FRONTIER | `1a-coder` |

### CLOUD_PLUS
| Band | Cascade |
|---|---|
| TRIVIAL | `1a-hearth` |
| MODERATE | `1a-hearth` → `3-fast` |
| COMPLEX | `3-gemini` |
| FRONTIER | `3-claude` |

### CLOUD_ONLY
| Band | Cascade |
|---|---|
| TRIVIAL | `3-fast` |
| MODERATE | `3-fast` |
| COMPLEX | `3-gemini` |
| FRONTIER | `3-claude` |

## Model notes

Model choice per tier is `config.py`'s `TIER_BACKENDS`, above. Two properties of the cascade
design are worth stating because they are not visible from one table:

- **A cascade escalates on availability, not on dissatisfaction.** A tier that answers ends the
  walk; the next tier is tried when the current one cannot be reached, not when its answer
  looks weak.
- **`WORKSHOP_OFFLINE` is the only mode with no cloud tier in any band.** Checking that
  property is a matter of reading its table, which is why all four of its rows are printed
  above rather than deferred to `config.py`.

## What this file said before 2026-11-13, and why each line is gone

It described the private deployment this release was derived from, not the `config.py` beside
it. Eight separate claims were wrong, and the tier names were wrong in a way that would have
made a reader's `X-Tier-Override` fail:

- **Tier names were given as `T1A_CODER`, `T1B`, `T2`, `T3_CLAUDE`** and so on. Those are the
  Python enum MEMBER names; the enum VALUES — the strings the API accepts — are `1a-coder`,
  `1b`, `2`, `3-claude`. A model following this file would have sent a tier that does not
  resolve.
- **A `T1A_OVERFLOW` tier** (`qwen3.5:4b`). No such tier exists in this release: `config.py`
  contains zero occurrences of `overflow`. It appeared in nine cascade cells.
- **A `T_NPU` tier** (`Qwen3-1.7b-FLM` on a Lemonade endpoint). The NPU classifier path is cut
  from this release — see `RELEASE-SCOPE.md` — and `config.py` contains no NPU tier. It led
  two JARVIS cascades.
- **A `JARVIS_OFFLINE` operating mode**, with its own four-row cascade table. Not one of the
  five modes in `config.py`. `WORKSHOP_OFFLINE` is this release's zero-egress mode.
- **`T1B` as a model named `bifrost-interactive`.** `1b` is `qwen2.5-coder:14b`.
- **`T2` as `qwen2.5-coder:14b`.** `2` is `qwen2.5-coder:32b`. The 14b figure was `1b`'s.
- **`T3_CLAUDE` as `claude-sonnet-4-6`.** The configured model is
  `claude-sonnet-4-5-20250929`.
- **A table of three named machines with their LAN addresses and port assignments.** Replaced
  by the endpoint-variable table above. The addresses were partially masked, which is not the
  same as absent, and the table described one deployment's topology as though it were a
  property of the software. Nothing in this image has a host to print: every endpoint is
  resolved from the environment or it raises.

A **"Model Family Rules"** section is also gone, in full. It compared named machines against
each other and quoted throughput figures with no stated basis — both of which this
repository's own `CLAUDE.md` forbids, in those words, for reasons that apply to this file
exactly as much as to the README. Whatever was true of two particular accelerators is not a
routing rule, and a reader cannot act on a tokens-per-second number whose configuration is
not given.
