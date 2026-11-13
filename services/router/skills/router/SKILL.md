---
name: bifrost-router
description: Routing, cascade configuration, and operating mode management for the BIFROST distributed inference platform. Use when working on config.py, cascade tables, OperatingMode enum, complexity band routing, tier assignments, JARVIS/WORKSHOP/OFFLINE mode logic, or any request that mentions routing tiers, escalation chains, or operating modes. Always use this skill when touching Router or Arbiter configuration, even if the user just asks a quick question about which tier handles a band.
resources:
  - name: cascade-tables
    type: file
    path: references/cascade-tables.md
---

# BIFROST Router Skill

The Router is the central dispatch layer. It classifies incoming requests by complexity band (TRIVIAL/MODERATE/COMPLEX/FRONTIER) and walks a cascade table to find the first available tier.

## Key files
- `config.py` — OperatingMode enum, CASCADE_TABLES, TIER_BACKENDS, RouterSettings
- `main.py` — FastAPI app, `/v1/chat/completions`, `/mode`, `/classify`, `/cascade/{band}`
- `classifier.py` — rule-based complexity classifier (keyword + token count)
- `strategies.py` — two_pass_stream, select_local_tier

## Machine label discipline
Every command must be prefixed: `[Bifrost]`, `[Hearth]`, or `[Forge]`. Never omit this.

## Critical constraints
- Python is NOT installed on Hearth — never give pip or python commands for Hearth
- Prefer single-line commands (multi-line paste has caused accidents)
- Files with BOM: use `encoding='utf-8-sig'`

## Cascade and tier reference
See `references/cascade-tables.md` for the tier map, all five modes' cascades, and the
endpoint VARIABLES. There is no IP/port table there any more and this line used to promise
one: every endpoint in this release resolves from the environment through
`shared/resolver.py`, which raises rather than defaulting, so there is no address to print.
That file also records the eight claims it carried that `config.py` contradicted — including
tier names that would have made an `X-Tier-Override` fail.

## Common tasks

### Add a new OperatingMode
1. Add enum value to `OperatingMode` in config.py
2. Add cascade table entry to `CASCADE_TABLES`
3. If zero-egress: ensure no cloud tiers appear in any band
4. Restart Router to pick up changes

### Change tier assignment
Edit `TIER_BACKENDS[Tier.X]["base_url"]` or `"model"` in config.py.
Slot assignments are fixed at hardware ceiling — don't conditionally swap.

### Check current cascade
```powershell
# [Bifrost]
curl http://localhost:8080/cascade/COMPLEX
```
