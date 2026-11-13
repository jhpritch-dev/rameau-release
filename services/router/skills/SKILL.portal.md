# SKILL: bifrost-portal

## What This Is
BIFROST Portal — React single-page app. Nordic Mission Control dark theme. Three surfaces: Observe (live fleet monitoring), Converse (streaming chat with RAG + routing pills), Command (mode control, AUTOPILOT launcher, profile switcher).

## Key Files
| File | Purpose |
|------|---------|
| `L:\bifrost-portal-src\src\BifrostPortal.jsx` | Single-file React app (source of truth) |
| `<repo-root>\bifrost-platform\BifrostPortal.jsx` | Repo copy (sync via Sync-GitHub.ps1) |
| `L:\build-portal.ps1` | Build script: npm build → copy to `L:\bifrost-portal\` |

## Live URL
`http://192.168.x.4:3110` (Hearth nginx, public via Tailscale funnel)

## Surfaces
| Surface | Status | Notes |
|---------|--------|-------|
| Observe | Live | Fleet PC cards, Prometheus metrics, loaded models |
| Converse | Live | Streaming + RAG + routing band/tier pills |
| Command | Pending | ModeControl, AUTOPILOT launcher, Profile Switcher stubs exist |

## Data Sources
- Broadcaster: `http://192.168.x.4:8092` — mode, tiers, /api/ps (loaded models + VRAM)
- Router: `http://192.168.x.33:8080` — /metrics, /status
- Observer: `http://192.168.x.4:8081` — k3d pod health

## Build + Deploy Pattern
```powershell
# [Bifrost or Hearth]
# Edit source at L:\bifrost-portal-src\src\BifrostPortal.jsx
# Then:
L:\build-portal.ps1
# Copies build output to L:\bifrost-portal\ (nginx serves from here)
# Then sync to repo:
cd <repo-root>\bifrost-platform  # or wherever your checkout lives
Sync-GitHub.ps1  # or manual git add/commit/push
```

## Design Language
- Nordic Mission Control dark theme
- Amber glow = privacy/JARVIS mode active
- NO CLOUD badge = WORKSHOP-OFFLINE/JARVIS-OFFLINE mode
- Routing pills: band label + tier label on each Converse response

## Pending (Command Surface)
- ModeControl component: mode dropdown, amber privacy glow, NO CLOUD badge, Arbiter API wiring
- Quick privacy toggle
- AUTOPILOT task submission wired to `/v1/chat/completions` with AUTOPILOT strategy
