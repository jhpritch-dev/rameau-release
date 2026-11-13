---
name: bifrost-portal
description: Nordic Mission Control Portal UI for BIFROST. Use when working on BifrostPortal.jsx, any Portal React components (BifrostBar, ModeControl, Converse, Observe, Command), the Portal build/deploy pipeline, or any visual/UX changes to the Portal. Always use when the user mentions the Portal, dashboard, Nordic theme, or any of the named surfaces.
---

# BIFROST Portal Skill

The Portal is the primary ops/chat/monitoring UI. Nordic Mission Control dark theme. Live at http://192.168.x.4:3110.

## Key paths
- Source: `L:\bifrost-portal-src\src\BifrostPortal.jsx`
- Monorepo mirror: `<repo-root>\bifrost-platform\bifrost-portal\src\BifrostPortal.jsx`
- Build script: `L:\build-portal.ps1`
- Deploy target: `L:\bifrost-portal\`

## Deploy pattern
```
[Bifrost] Downloads → L:\src → build → L:\bifrost-portal\ → copy to monorepo → git push
```
Always run build script, never edit deploy target directly.

## Surfaces
- **Observe**: Live — fleet health, tier status, Prometheus metrics
- **Converse**: Live — streaming chat, RAG, routing pills, tier override chips
- **Command**: Pending — ModeControl, AUTOPILOT submission, Ops Agent approval gate

## ModeControl component
File: `ModeControl.jsx` (integrate into BifrostBar)
- Wires to Arbiter :8082 (`POST /privacy`, `POST /mode`)
- JARVIS_OFFLINE activates amber glow + NO CLOUD badge + viewport flash
- `arbiterUrl="http://192.168.x.33:8082"` (Bifrost, until Session S moves to Hearth)

## Design rules
- Nordic Mission Control dark theme — no light surfaces
- Routing pills use tier color coding consistent with TopNav strip
- Amber = privacy/offline mode accent color
- All API calls use explicit IPs (192.168.2.x), never hostnames
