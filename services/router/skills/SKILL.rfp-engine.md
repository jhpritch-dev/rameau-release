# SKILL: bifrost-rfp-engine

## What This Is
RFP Engine — privacy-first autonomous proposal generator. Uses BIFROST Router in INTERACTIVE mode for sequential visible section drafts. Primary commercial demo target. Zero cloud egress for compliance markets (insurance, legal, healthcare).

## Status
Phase 1 (INTERACTIVE sequential) built. Phase 2 (full AUTOPILOT RAG pipeline) pending Session I.

## Key Files
| File | Purpose |
|------|---------|
| `D:\Projects\bifrost-router\Test-RFPEngine-v1.ps1` | Smoke test script — five sections, INTERACTIVE mode |
| `D:\Projects\bifrost-router\rfp-outputs\` | Draft output landing directory |

## Demo Document
KIPDA Employee Benefits Broker RFP — five sections, ideal for Chris Duncan (insurance/benefits vertical). Produces timestamped draft with tier routing pills showing zero cloud egress.

## Phase 1 Pipeline (Current)
```
RFP document → five section prompts → Router INTERACTIVE
    → each section: classify → route to appropriate tier → draft
    → output: timestamped file at rfp-outputs\
    → visible: which tier handled which section
```

## Phase 2 Pipeline (Session I)
```
RFP drop → watched folder ingestion
         → Tika/Gotenberg PDF/DOCX extraction
         → section-aware chunking
         → Vega 8 document classification
         → ChromaDB embeddings (Hearth bifrost-kb)
         → AUTOPILOT decomposition
         → RAG-grounded section drafts (past proposals as context)
         → assembled proposal with source attribution
```

## Target Verticals
- Insurance/benefits (Chris Duncan — EPIC/Decisely connection)
- Legal (zero-egress NDA compliance)
- Healthcare (HIPAA — no PHI leaves network)
- Professional services (general proposal work)

## Running Phase 1 Smoke Test
```powershell
# [Bifrost] — Router must be running on :8080
& "D:\Projects\bifrost-router\Test-RFPEngine-v1.ps1"
# Output at D:\Projects\bifrost-router\rfp-outputs\
```

## Commercial Angle
Privacy-first compliance = routing all inference locally. NO CLOUD badge in Portal is the proof point. Demo flow: Portal amber mode → script runs → output shows zero cloud tokens → Chris Duncan sees the value prop.
