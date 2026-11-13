---
name: bifrost-rfp-engine
description: Privacy-first autonomous RFP/proposal generation engine built on BIFROST. Use when working on the RFP ingestion pipeline, document parsing, proposal generation, the KIPDA demo, or any work targeting the insurance/benefits/professional-services vertical. Use when the user mentions RFP, proposal, Chris Duncan, KIPDA, or zero-egress compliance.
---

# BIFROST RFP Engine Skill

The RFP Engine is the primary near-term commercial application. It ingests RFP documents, extracts requirements, and generates compliant proposal responses — entirely on local inference (zero cloud egress for privacy-sensitive clients).

## Target verticals
- Insurance / employee benefits (Chris Duncan contact — former VP Risk Mgmt, Decisely co-founder)
- Professional services, legal, healthcare — any zero-egress compliance market

## Demo document
KIPDA Employee Benefits Broker RFP — identified as ideal first demo for Chris Duncan.

## Architecture (planned)
```
RFP PDF → OCR/parse → RequirementsExtractor (T2_5) → SectionRouter
        → SectionWriter × N (AUTOPILOT subtask per section)
        → ComplianceReviewer (T2_5, review_tier_exclusion enforced)
        → Assembler → final proposal document
```

## Key constraints
- Zero cloud egress in JARVIS_OFFLINE — all inference stays local
- Reviewer tier must differ from generating tier (review_tier_exclusion config)
- Output format: DOCX (use docx skill for generation)

## Status
Demo 3 recorded. Full ingestion pipeline still needed for production demo.
KIPDA RFP identified — ingest pipeline is the next build gate.
