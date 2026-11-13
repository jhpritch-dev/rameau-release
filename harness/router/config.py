"""
BIFROST Complexity Router â€” Configuration
==========================================
Mode-aware routing configuration. The Router adapts its cascade
logic based on the current operating mode (from Broadcaster) and
available hardware.
"""

from enum import Enum
from pydantic import BaseModel
from typing import Optional
import os
from pathlib import Path
from collections.abc import Mapping as _Mapping  # v122 hot-reload proxy
from shared.resolver import resolve

# ---------------------------------------------------------------------------
# NPU classifier configuration (v98+)
# ---------------------------------------------------------------------------
# NPU_ROUTING_ENABLED values:
#   live    -- NPU drives routing when confidence >= NPU_CONFIDENCE_THRESHOLD;
#              falls back to rule on low-conf, timeout, or error.
#   shadow  -- NPU runs but rule decides (telemetry-only, v96/v97 behavior).
#   off     -- skip NPU call entirely; rule decides.
NPU_TIMEOUT_S = int(os.getenv("NPU_TIMEOUT_S", "8"))  # 8s: right-sized for live model qwen3:1.7b (cold ~6.96s / warm ~2.19s measured 2026-06-25). The prior 15s was provisioned for the now-retired gemma3:1b (~16s cold); 8s covers qwen3 cold-load with margin and halves the worst-case routing stall before rule fallback (fail-fast intent, npu-classifier-embed brief). NOTE: int() parse -- value must be a whole number of seconds. ENV NAME: reads unprefixed NPU_TIMEOUT_S (the name set on the live deployment + used in the npu-classifier-timeout brief/memory) -- was BIFROST_NPU_TIMEOUT_S, which nothing set, so the env var was inert and 8s held only by this default coincidence (fixed T3E 2026-06-26).
NPU_CONFIDENCE_THRESHOLD = float(os.getenv("BIFROST_NPU_CONF_THRESHOLD", "0.75"))
NPU_ROUTING_ENABLED = os.getenv("BIFROST_NPU_ROUTING", "live").lower()  # live | shadow | off


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComplexityBand(str, Enum):
    TRIVIAL = "TRIVIAL"
    MODERATE = "MODERATE"
    COMPLEX = "COMPLEX"
    FRONTIER = "FRONTIER"


class OperatingMode(str, Enum):
    JARVIS = "JARVIS"
    WORKSHOP = "WORKSHOP"
    WORKSTATION = "WORKSTATION"
    REMOTE = "REMOTE"
    NOMAD = "NOMAD"
    CLOUD_PLUS = "CLOUD_PLUS"
    CLOUD_ONLY = "CLOUD_ONLY"
    WORKSHOP_OFFLINE = "WORKSHOP_OFFLINE"
    JARVIS_OFFLINE = "JARVIS_OFFLINE"  # full fleet, zero cloud egress


class Tier(str, Enum):
    T1A_CODER = "1a-coder"
    # RETIRED 2026-09-11 (tiers PENDING, after M2b removed its alias bifrost-coder-tool): member
    # T1A_CODER_TOOL, value "1a-coder-tool". No alias, in no CASCADE_TABLE, 0 of 2,000 telemetry
    # events. Removed with its fleet_config.json tier; main.py answers an X-Tier-Override naming it
    # with 400 tier_retired (_RETIRED_TIER_OVERRIDES).
    T1A_OVERFLOW = "1a-overflow"
    T1A_HEARTH = "1a-hearth"
    # GATE 2 candidate tiers (2026-08-28, signed). Addressable-ONLY: absent from every
    # CASCADE_TABLE below, so no classifier decision and no bifrost-auto request can land
    # here. Backends come from fleet_config.json via tier_map = {t.value: t for t in Tier}.
    T1A_HEARTH_CAND_G42 = "1a-hearth-cand-g42"  # granite4.2:3b @ Hearth :11434
    T1A_HEARTH_CAND_G41 = "1a-hearth-cand-g41"  # granite-4.1-8b-tekken:latest @ Hearth :11434
    T1A_INSTRUCT = "1a-instruct"
    T1B = "1b"
    T2 = "2"
    T2_5 = "2.5"
    T2_5_SOLO = "2.5-solo"  # v130 Igor 65k solo mode: gpt-oss-igor-65k, mutually exclusive with T2_5 due to 96GB UMA constraint
    T3_CLAUDE = "3-claude"
    T3_GEMINI = "3-gemini"
    T3_FAST = "3-fast"
    T_NPU    = "npu"
    # RETIRED 2026-09-11 (tiers PENDING, after M2b removed its alias bifrost-npu-tools): member
    # T_NPU_TOOLS, value "npu-tools" -- the NPU-TOOL-LEAF Phase 1 read-only tool leaf (2026-07-23),
    # addressable-only and in no CASCADE_TABLE. Removed with its fleet_config.json tier and its
    # _TOOL_LOOP_BUDGETS row; main.py answers an X-Tier-Override naming it with 400 tier_retired.
    # T_NPU above (the classifier tier, alias bifrost-npu) STAYS.
    T_SEARCH = "search"   # Context-1: 20B multi-hop RAG scout (Bifrost 9070XT)
    T_N8N_OPS = "n8n-ops"  # n8n workflow automation backend (Hearth docker-compose)

    # v114.7 ROUTER-ALIAS-PINNING-2 (2026-05-19) — new tiers for toolmaster aliases
    # + bridge to existing fleet_config "forge-t1" entry that lacked enum coverage.
    T1_FORGE      = "forge-t1"     # Bridges existing fleet_config tier; bifrost-forge-t1 alias
    T2_DEVSTRAL   = "2_devstral"   # Devstral 24B @ Forge :11434 — NO alias points here since R-14 (2026-08-12)
    T2_QWEN_CODER = "2_qwen_coder" # qwen3-coder:30b-a3b-q4_K_M @ Forge :11434 — bifrost-coder-toolmaster (Bucket B)
    T2_QWEN_CODER_KILO2 = "2_qwen_coder_kilo2"  # 32k ctx, two-Kilo topology

    # P2 KAT-LONG (2026-08-14) -- Kilo Code long-context slot. 65,536 ctx MEASURED on
    # Forge (22.8GB VRAM at that window), chosen so it co-schedules with gpt-oss-igor-65k
    # rather than evicting Session O's proposer. This member is REQUIRED: fleet_config
    # carries tier 2_kat_long, and preflight tier-enum-subset blocks the build without it
    # (a missing member surfaces only as a deploy-time crashloop, cf. v205).
    T2_KAT_LONG = "2_kat_long"

    # CEREMONY-96 (2026-08-23): gpt-oss-20b 16k on Forge :11434, 13.79GB on disk,
    # MXFP4, 20.9B. Its ollama TEMPLATE is 7216 chars and renders .Tools and .System
    # -- MEASURED, not taken from `capabilities`, which lies (it claimed 'tools' for
    # coder-toolmaster on a 13-char passthrough template). At a fifth of Igor-65k's
    # 64.5GB it CO-RESIDES with Igor on the 124GB UMA rather than evicting it, so it
    # does not inherit the T2.5/T2.5-solo mutual exclusion. Addressable ONLY: absent
    # from every CASCADE_TABLE, so nothing auto-routes here.
    T2_PANEL_OSS20B = "2_panel_oss20b"

    # 2026-09-19 (John: "bind it alongside until cross tested"). CohereLabs
    # North-Mini-Code-1.0, 30.5B total / 3B active MoE (family cohere2moe), Q4_K_M, on Forge.
    # Addressable ONLY: absent from every CASCADE_TABLE, so nothing auto-routes here, and
    # deliberately NOT bound to tier 2 -- T2 is the multimodal VERDICT judge drafting the
    # research and general profiles, and this model has no vision. Measured 57-64 tok/s vs the
    # 48.15 baseline; callers should send think:false, which suppresses its reasoning cleanly.
    T2_NORTH_CODE = "2_north_code"

    # v114.8 MISTRAL-T2-GENERAL-1 (2026-05-19) — Bucket A general local-fast on Bifrost RDNA4 16GB.
    # Path B canonical Ollama lib + Modelfile num_ctx=2048; HF GGUF Q3 Path A REFUTED 0/3 native
    # (memory #230 parser-fix hypothesis closed empirically — HF mirror lacks tool chat template).
    T2A_BIFROST_MISTRAL = "2a_bifrost_mistral"  # mistral-small3.2-bifrost @ Bifrost :11434 — bifrost-t2-general

    # v114.9 EMBED-HANDLER-1 + CONFIG-DRIFT-CLEANUP-1 (2026-05-19)
    # Three orphan tier bridges — closes #228/#235 drift category to zero.
    # Bridge pattern: enum string-value == existing fleet_config tier string.
    T_EMBED_HEARTH  = "embed-hearth"     # Hearth :11436 nomic-embed-text (Vega8 iGPU instance) — embed-handler dispatch
    T1A_FORGE       = "1a-forge"         # Forge T1A (existing fleet_config entry; previously enum-orphan)
    # T2_LLAMASERVER  = "2-llamaserver"    # REMOVED v125 hygiene sweep (2026-05-23): the deprecated separate-tier entry was made redundant when v124 flipped the real T2 (tier "2") to llama_server; no live dispatcher used T2_LLAMASERVER, only the validation set in autopilot_graph and a comment in main.py referenced it (both cleaned up in v125).

    # v115 V115-OPENROUTER-1 (2026-05-20) — cloud cascade via OpenRouter.
    # OpenAI-compat backend already implements Bearer auth (openai_compat.py:122-124);
    # this enum entry + PROVIDER_DEFAULTS["openrouter"] registration + fleet_config "3-openrouter"
    # tier completes wiring. bifrost-openrouter alias pins here. Default model anthropic/claude-sonnet-4-5.
    T3_OPENROUTER   = "3-openrouter"     # OpenRouter cloud cascade — Bearer auth via OPENROUTER_API_KEY env from k8s secret openrouter-api

    # v130 feat(tiers): add profile-tagged OpenRouter free tiers (agent/research/chat profiles; coder-super 480B anchor)
    T3_CODER_SUPER  = "3-coder-super"    # OpenRouter 480B coder tier with profile=agent, role=synthesis
    T3_SYNTHESIS    = "3-synthesis"      # OpenRouter 120B synthesis tier with profile=research, role=assembly
    T3_REASONING    = "3-reasoning"      # OpenRouter 80B reasoning tier with profile=research, role=evaluation
    T3_VISION       = "3-vision"         # OpenRouter 12B multimodal tier with profile=any, role=multimodal
    T3_LITE_FREE    = "3-lite-free"      # OpenRouter 20B chat tier with profile=chat, role=overflow
    T3_GLM52        = "3-glm52"          # OpenRouter z-ai/glm-5.2:free, escalation_only, direct-address (W4b 2026-09-18)

    # v129 fix: enum entries paired with v128 fleet_config additions. escalation_only=true
    # in fleet_config -- not yet wired into PROVIDER_DEFAULTS or k8s secrets; keys activate later.
    # Drift-zero invariant: every fleet_config tier ID must have a Tier enum entry.
    T3_CEREBRAS     = "3-cerebras"       # v128 stub. Cerebras ultra-fast inference. CEREBRAS_API_KEY pending.
    T3_PERPLEXITY   = "3-perplexity"     # v128 stub. Perplexity web-grounded. PERPLEXITY_API_KEY pending.
    T3_COHERE       = "3-cohere"         # v128 stub. Cohere RAG-native. COHERE_API_KEY pending.
    T3_TOGETHER     = "3-together"       # v128 stub. Together AI overflow. TOGETHER_API_KEY pending.
    T3_GROK         = "3-grok"           # v128 stub. xAI Grok architect-alt. XAI_API_KEY pending.

    # ── Future node placeholders (uncomment when hardware arrives) ──
    # T1A_FORGE2  = "1a-forge2"   # Second Forge node Tier 1a
    # T2_FORGE2   = "2-forge2"    # Second Forge node Tier 2
    # T2_5_FORGE2 = "2.5-forge2"  # Second Forge node Tier 2.5
    # T1A_NODE4   = "1a-node4"    # Fourth node Tier 1a (generic)
    # T2_NODE4    = "2-node4"     # Fourth node Tier 2 (generic)


class BifrostProfile(str, Enum):
    B_LIGHT = "B-Light"
    B_DUAL = "B-Dual"
    B_HEAVY = "B-Heavy"


class RoutingStrategy(str, Enum):
    INTERACTIVE = "INTERACTIVE"
    TWO_PASS = "TWO_PASS"
    AUTOPILOT = "AUTOPILOT"


# ---------------------------------------------------------------------------
# Tier â†’ Backend mapping
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fleet config hot-reload shim (Session T)
# fleet_config.json is the source of truth — edit it to change tier assignments
# without restarting the Router. Reloads on mtime change every request.
# ---------------------------------------------------------------------------

_FLEET_CONFIG_PATH = Path(__file__).parent / "fleet_config.json"
_fleet_config_mtime: float = 0.0
_fleet_config_cache: dict = {}


def _load_fleet_config() -> dict:
    """Load fleet_config.json, cache until mtime changes."""
    global _fleet_config_mtime, _fleet_config_cache
    import json
    try:
        mtime = _FLEET_CONFIG_PATH.stat().st_mtime
        if mtime != _fleet_config_mtime:
            with open(_FLEET_CONFIG_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM silently
                _fleet_config_cache = json.load(f)
            _fleet_config_mtime = mtime
    except Exception as e:
        import logging
        logging.getLogger("bifrost.config").warning(f"fleet_config.json load failed: {e}")
    return _fleet_config_cache


def get_pipeline_config() -> dict:
    """Read pipeline_mode from fleet_config.json. An absent block reads as disabled.

    Reuses _load_fleet_config(), so it inherits the existing mtime hot-reload. Note that
    fleet_config.json is BAKED into the image, not mounted: the reload is real, but an in-pod
    edit is drift that a pod restart discards. Enabling is a rebuild + rollout.
    """
    return (_load_fleet_config() or {}).get("pipeline_mode", {"enabled": False})


def get_fanout_config() -> dict:
    """Read quality_weighted_fanout from fleet_config.json. Absent block reads as disabled.

    Ships in the SAME change as the code that consumes it. fleet_config.json already carries
    a block nothing reads (profiles_catalog, line 1235); a second one would look configured,
    review as configured, and do nothing.
    """
    return (_load_fleet_config() or {}).get("quality_weighted_fanout", {"enabled": False})


def _build_tier_backends(fleet: dict) -> dict:
    """Convert fleet_config.json tiers list to TIER_BACKENDS dict format."""
    result = {}
    tier_map = {t.value: t for t in Tier}
    for entry in fleet.get("tiers", []):
        tier_str = entry.get("tier")
        if tier_str not in tier_map:
            continue
        tier = tier_map[tier_str]
        backend = {
            "type": entry["type"],
            "model": entry["active_model"],
        }
        if entry.get("base_url"):
            backend["base_url"] = entry["base_url"]
        if entry.get("provider"):
            backend["provider"] = entry["provider"]
        # MACHINE (D9 COMPARISON, applied 2026-10-16): which physical machine serves this
        # tier. Surfaced so autopilot_graph can select the per-machine concurrency semaphore
        # from IDENTITY instead of substring-matching base_url for a host address.
        #
        # WHY THIS KEY EXISTS AT ALL. The old branch tested three IP literals against
        # base_url. The 2026-10-05 genericize rewrote both the literals and the config in one
        # pass, so they still agreed and nothing broke -- but the moment base_url carries a
        # real address again (which is what _resolve_fleet_host does, D19) the tests stop
        # matching and every derived tier falls to the cloud arm. An address is a deployment
        # fact: it changes under re-IP, rename, DHCP and publication scrubbing, none of which
        # get code review. `machine` is identity and survives all four.
        #
        # Not a new vocabulary: fleet_config.json carries `machine` on 38 of 38 tiers with
        # four values (forge/hearth/bifrost/cloud), and main.py:4519 and :4636 already route
        # on it to decide what to poll and what never to bill. telemetry.py's header states
        # the same rule -- that "which host served it" is answered by joining the tier to
        # fleet_config.json tiers[].machine.
        #
        # Absence is deliberately NOT defaulted here. A tier with no `machine` reaches
        # autopilot_graph as None and refuses router startup, which is the documented stance
        # of _derive_tier_registry: wrong concurrency caps corrupt results quietly, a refusal
        # to boot is obvious.
        if entry.get("machine"):
            backend["machine"] = entry["machine"]
        # ROUTER-NUMCTX (2026-05-29): per-tier context window. Surfaced so the
        # Ollama backend can set options.num_ctx per request and stop models
        # inheriting the tiny Modelfile default (num_ctx=2048 fleet-wide) that
        # silently truncated long agent prompts into empty completions.
        if entry.get("num_ctx"):
            backend["num_ctx"] = entry["num_ctx"]
        # KEEP-ALIVE (A2 2026-06-13; T2 exception recorded 2026-07-07): per-tier keep_alive
        # override. A tier may set "_keep_alive_override" to pin or drop its model; tiers that
        # omit it (or set null) inherit the backend's global keep-alive -- OLLAMA_KEEP_ALIVE=30m
        # on Forge.
        # NOTE: T2 (gemma4 on Forge) is deliberately null, NOT "0". Its override was dropped
        # 2026-07-07 so gemma4 stays resident as the EVALUATOR core; Ollama LRU evicts it when
        # Igor loads. Do not "restore" a "0" here -- see fleet_config.json "_keep_alive_note"
        # on the tier 2 entry.
        # Surfaced here so backends/ollama.py can read backend["_keep_alive_override"].
        if entry.get("_keep_alive_override") is not None:
            backend["_keep_alive_override"] = entry["_keep_alive_override"]
        result[tier] = backend
    return result


def get_tier_backends() -> dict:
    """Get current TIER_BACKENDS from fleet_config.json (hot-reloaded on mtime change)."""
    fleet = _load_fleet_config()
    if fleet:
        return _build_tier_backends(fleet)
    # 2026-08-22 (PACKET-85, option B). There is no hardcoded fallback any more.
    # It named 13 of 35 tiers, drifted silently, and was reachable only on a COLD START
    # with fleet_config.json missing or corrupt -- a running Router keeps its cache.
    # A Router that refuses to start is a loud, correct failure. One that starts with 13
    # of 35 tiers backed is a quiet one that fails later, per-tier, on live traffic.
    raise RuntimeError(
        "fleet_config.json is missing or unreadable and there is no fallback backend map. "
        "Refusing to serve with an incomplete tier table. Restore %s and restart."
        % _FLEET_CONFIG_PATH)


# v122 (2026-05-23): hot-reload restored via collections.abc.Mapping proxy.
# The prior `property()` line at this site was a dead store (Python `property`
# only fires for class attribute access, not module-level), and the fallback
# `TIER_BACKENDS = get_tier_backends()` snapshot at import time froze the dict
# for the lifetime of the uvicorn process -- defeating fleet_config hot-reload
# on the dispatch path. main.py:1769/1812/2513 + backends/ollama.py:39/94 +
# autopilot_graph.py:635 all read `TIER_BACKENDS[tier]` and saw the snapshot.
#
# Now: `from config import TIER_BACKENDS` imports a singleton proxy; every
# `TIER_BACKENDS[tier]` / iteration / `.get()` / `in` / `**` re-resolves via
# get_tier_backends(), which mtime-caches the fleet_config.json read for
# efficiency. fleet_config edits via kubectl cp now propagate to the live
# dispatch path without an image rebuild.
#
# Empirically proven on v125 (pre-fix): cp'd active_model change reached the
# on-disk file + fresh `get_tier_backends()` + a freshly-imported config in a
# separate process, but did NOT reach the long-lived uvicorn's /cascade
# endpoint (which uses TIER_BACKENDS[t]). v122+ closes that gap.
class _LiveTierBackends(_Mapping):
    """Live read-through Mapping over get_tier_backends(). Implements just
    __getitem__/__iter__/__len__; _Mapping ABC supplies .get(), 'in',
    .keys(), .items(), .values(), __eq__, and ** unpacking for free."""
    def __getitem__(self, key):
        return get_tier_backends()[key]
    def __iter__(self):
        return iter(get_tier_backends())
    def __len__(self):
        return len(get_tier_backends())

TIER_BACKENDS = _LiveTierBackends()


# ---------------------------------------------------------------------------
# Capability registry (Session: capabilities)
# Reads `capabilities` from each tier in fleet_config.json. Hot-reloaded via
# the same mtime cache as get_tier_backends(). Backward-compatible: tiers
# without a capabilities block are reported as all-false.
# ---------------------------------------------------------------------------

_DEFAULT_CAPS = {"thinking": False, "vision": False, "tool_use": False}


def get_tier_capabilities(tier) -> dict:
    """Return the capability dict for a Tier (enum or string)."""
    tier_str = tier.value if hasattr(tier, "value") else str(tier)
    fleet = _load_fleet_config()
    if not fleet:
        return dict(_DEFAULT_CAPS)
    for entry in fleet.get("tiers", []):
        if entry.get("tier") == tier_str:
            caps = entry.get("capabilities") or {}
            return {**_DEFAULT_CAPS, **caps}
    return dict(_DEFAULT_CAPS)


# ---------------------------------------------------------------------------
# Mode â†’ Cascade routing tables
# ---------------------------------------------------------------------------
# Each band maps to an ordered list of tiers to try.
# The Router walks the list until one succeeds or all fail.

# 2026-10-01 (John, APPROVED): the band PIPELINE. trivial->Hearth, moderate->Bifrost/T1B,
# complex->Forge/T2 gemma, frontier->Forge/T2_5 igor, frontier escalation->the OpenRouter
# FREE lane before any paid tier. The four heads were ALREADY these tiers in JARVIS; what
# this change did was (a) make the other six local modes agree, because DEFAULT_CASCADE_MODE
# is WORKSHOP and WORKSHOP led MODERATE with the 4B, so a mode switch silently moved MODERATE
# off Bifrost, and (b) SHORTEN MODERATE from six legs to three.
# Measured 2026-10-01 before the change: MODERATE landed on EIGHT distinct tiers over 126
# rows -- 22.2% on the 4B qwen3.5-4b -- which was the TAIL walking, not the head misfiring.
# The OFFLINE modes deliberately get NO cloud leg: JARVIS_OFFLINE is what the demo bank is
# pinned to, and a free cloud escalation there would change what the bank measures.
# Verified before approval: all four CASCADE_TABLES consumers are index- and length-agnostic
# (main.get_cascade, autopilot_graph._cascade_tiers, commands.cmd_cascade,
# subtask_graph.get_cascade), so MODERATE going 6 legs -> 3 breaks no caller.
# COST, stated: three legs instead of six means a Bifrost AND Forge outage leaves MODERATE
# with only the free cloud lane. Fewer silent downgrades, less headroom. The preimage's
# verify CANNOT detect this; only an outage will.
# CEREMONY 118 (2026-10-13), John: "remove 3-lite-free from the MODERATE cascade". Removed
# from MODERATE in EVERY mode (JARVIS, WORKSHOP, CLOUD_PLUS, CLOUD_ONLY, WORKSTATION, REMOTE,
# NOMAD; the two *_OFFLINE modes never carried it). The tier itself and FRONTIER are NOT
# changed. This SUPERSEDES the Ceremony 99 note below that WORKSHOP/WORKSTATION tests
# "correctly expect 3-lite-free": they now end MODERATE at T2, as the *_OFFLINE modes do.
# JARVIS/REMOTE/NOMAD keep T3_FAST (groq) as the terminal cloud leg, so the _LAST_RESORT_TIERS
# demotion simply has nothing to demote in MODERATE any more.
# COST, stated: WORKSHOP (the class default) and WORKSTATION now have NO cloud leg in
# MODERATE, so a Bifrost (1b) AND Forge (2) outage leaves MODERATE with no tier at all,
# where before it fell to the free overflow tier. CLOUD_ONLY MODERATE is one leg (3-fast).
CASCADE_TABLES = {
    OperatingMode.JARVIS: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        # 2026-08-10: TERMINAL decorrelation leg (signed packet, variant A). Fires ONLY after all
        # five local tiers have failed. The signed text claimed "1b / 2 / the 2.5 code-hop all sit
        # on Forge"; CORRECTED 2026-09-29 -- 1b is granite on BIFROST, so a Forge outage (#46)
        # still leaves four local tiers across Bifrost and Hearth. JARVIS ONLY -- JARVIS_OFFLINE
        # and WORKSHOP_OFFLINE stay pure-local so the demo bank keeps measuring local capability
        # (bank pinned to JARVIS_OFFLINE in the same change).
        # CEREMONY 99 (2026-10-03): T3_FAST RESTORED to MODERATE here and in REMOTE and
        # NOMAD -- the three blocks 88fee57 (2026-10-01, 'the band pipeline across all
        # local modes') dropped it from. WORKSHOP and WORKSTATION are deliberately NOT
        # touched: neither ever carried this leg, and their tests correctly expect
        # 3-lite-free. Other bands were unaffected; the drop was MODERATE-only.
        #
        # Why it had to come back: with no non-last-resort cloud candidate left in the
        # chain, subtask_graph's _LAST_RESORT_TIERS demotion (:3812, applied as a sort
        # key at :3985) had nothing to demote 3-lite-free BELOW, so the guard was INERT
        # and the free overflow tier BECAME the terminal leg that 870b4c2 and 15b0a84
        # signed it never to be. The guard was never broken -- its alternative had been
        # deleted, so nothing could page and only three tests saw it.
        #
        # It also defeated the decorrelation stated just above: 3-lite-free shares ONE
        # per-ACCOUNT OpenRouter quota with the other five _FREE_BUDGET_TIERS and is the
        # fleet's heaviest-used tier (174 of 305 requests on 2026-10-03), so it fails
        # TOGETHER with the primary cloud path; groq is a different vendor and is not in
        # that bucket at all. Production also measured 3-lite-free at a 60.9s median.
        # FRONTIER is NOT changed: T3_CLAUDE/T3_REASONING already follow 3-lite-free
        # there, so the guard has alternatives and fires correctly.
        # Per this table's own NOTE below, REMOTE and NOMAD mirror JARVIS by intent, and
        # they are the two that lost the leg with it -- so they follow it back.
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    OperatingMode.WORKSHOP: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    OperatingMode.WORKSHOP_OFFLINE: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5],
        ComplexityBand.FRONTIER: [Tier.T2_5],
    },
    OperatingMode.CLOUD_PLUS: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH],
        ComplexityBand.MODERATE: [Tier.T1A_HEARTH, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T3_GEMINI, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    OperatingMode.CLOUD_ONLY: {
        ComplexityBand.TRIVIAL:  [Tier.T3_FAST],
        ComplexityBand.MODERATE: [Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T3_GEMINI, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    # WORKSTATION: Forge-primary. Hearth carries the small work, Forge the large, Bifrost's
    # 1b stays available. CONFIRMED 2026-08-17: the Portal MODE_DESC says "Bifrost offline",
    # arbiter's MODE_TIER_POLICY lists "1b", and John ruled for arbiter -- the 9070 XT is an
    # essential leg, not overflow. The Portal string is the stale one. T1B sits in MODERATE
    # and above, never TRIVIAL, matching the JARVIS convention.
    OperatingMode.WORKSTATION: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    # REMOTE: thin client via Tailscale; the full stack is reachable through the tunnel, so
    # nothing is excluded. Deliberately mirrors JARVIS (verified identical in all four bands
    # against the live v357 table on 2026-08-17). CONFIRMED 2026-08-17.
    OperatingMode.REMOTE: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    # NOMAD: portable, but TUNNELLED -- it reaches the fleet over Tailscale rather than being
    # cut off from it. CONFIRMED 2026-08-17 by direct observation: John worked from Nomad with
    # a live SSH session to Bifrost and RDP to Hearth. An earlier draft of this block excluded
    # every Hearth/Bifrost tier on the strength of the Portal's "disconnected from the LAN"
    # text; that text is wrong. Same shape as REMOTE for the same reason.
    OperatingMode.NOMAD: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
        ComplexityBand.FRONTIER: [Tier.T2_5, Tier.T3_LITE_FREE, Tier.T3_CLAUDE, Tier.T3_REASONING],
    },
    # NOTE: WORKSTATION, REMOTE and NOMAD above are written out in full rather than aliased to
    # JARVIS. REMOTE and NOMAD are intentional mirrors of JARVIS as of 2026-08-17 -- if the
    # JARVIS cascade is ever changed, decide explicitly whether these two follow it.
    # JARVIS_OFFLINE: full fleet, zero cloud egress. Tops at T2_5.
    OperatingMode.JARVIS_OFFLINE: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1B, Tier.T2],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5],
        ComplexityBand.FRONTIER: [Tier.T2_5],  # no cloud escalation
    },
}

# Modes not explicitly listed fall back to WORKSHOP cascades
DEFAULT_CASCADE_MODE = OperatingMode.WORKSHOP


# ---------------------------------------------------------------------------
# Classifier thresholds (rule-based, Phase 1)
# ---------------------------------------------------------------------------

class ClassifierConfig(BaseModel):
    """Tunable thresholds for the rule-based complexity classifier."""

    # Token count thresholds
    trivial_max_tokens: int = 200
    moderate_max_tokens: int = 1500
    complex_max_tokens: int = 8000
    # Above complex_max_tokens â†’ FRONTIER

    # File reference thresholds
    multi_file_threshold: int = 3  # â‰¥3 files referenced â†’ bump up one band

    # Keyword weights (additive score)
    trivial_keywords: list[str] = [
        "autocomplete", "complete", "fill", "snippet", "boilerplate",
        "getter", "setter", "import", "require",
    ]
    moderate_keywords: list[str] = [
        "implement", "function", "method", "class", "test", "fix",
        "bug", "error", "docstring", "type", "interface", "endpoint",
        "handler", "validate", "parse", "decorator", "retry", "http",
        "backoff", "logging", "config", "serializ", "deserializ",
        "convert", "transform", "filter", "sort", "search", "crud",
        "api", "request", "response", "client", "server", "model",
        "util", "helper", "wrapper", "abstract", "generic", "template",
        # v92 (2026-04-25): question/explanation patterns for non-code prose
        # that previously fell through to TRIVIAL and routed to NPU/1a-overflow.
        "explain", "compare", "describe", "summarize", "define",
        "difference", "differences", "why", "how does", "what is",
        "what are", "when should", "should i",
        # 2026-07-03 routing-miss fix: prose/creative/how-to intents that
        # fell to TRIVIAL and mis-routed to NPU (French Revolution / poem /
        # "how to center a div" all under-routed). Bump to MODERATE.
        "how to", "how do", "how can", "write", "poem", "story",
        "essay", "draft", "tell me", "walk me through",
    ]
    complex_keywords: list[str] = [
        "refactor", "redesign", "migrate", "multi-file", "architecture",
        "system", "pipeline", "workflow", "integration", "database",
        "schema", "performance", "optimize", "concurrent", "async",
        "middleware", "algorithm", "redis", "cache", "queue",
        "authentication", "authorization", "oauth", "saml", "jwt",
        "websocket", "streaming", "batch", "scheduler", "cron",
        "rate limit", "throttle", "session", "federation",
        "thread-safe", "thread_safe", "prometheus", "metrics", "priority queue",
        "token bucket", "per-user", "rate limiting", "concurrency",
        "semaphore", "mutex", "deadlock", "race condition", "atomic",
        "tradeoff", "trade-off",
        "design", "privacy", "inference architecture",
    ]
    frontier_keywords: list[str] = [
        "system architect", "design system", "security audit",
        "threat model", "distributed", "consensus", "novel",
        "from scratch", "entire", "codebase", "review all",
        "byzantine", "fault tolerance", "formal proof", "correctness",
        "compiler", "language design", "type system", "kernel",
    ]


# ---------------------------------------------------------------------------
# Router settings
# ---------------------------------------------------------------------------

class RouterSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    current_mode: OperatingMode = OperatingMode.WORKSHOP
    bifrost_profile: BifrostProfile = BifrostProfile.B_DUAL
    default_strategy: RoutingStrategy = RoutingStrategy.INTERACTIVE

    # Broadcaster endpoint (for dynamic mode updates)
    broadcaster_url: Optional[str] = resolve("broadcaster")
    arbiter_url: Optional[str] = resolve("arbiter")

    # Anthropic API (Tier 3-Claude â€” future)
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-6"

    # Gemini API (Tier 3-Gemini â€” default cloud)
    gemini_api_key: Optional[str] = None

    # Groq API (Tier 3-Fast â€” free fast fallback)
    groq_api_key: Optional[str] = None

    # Ollama
    ollama_base_url: str = resolve("ollama")

    # ROUTER-NUMCTX (2026-05-29): safe global fallback context window for any
    # Ollama tier that does not declare its own num_ctx in fleet_config.json.
    # 8192 fits a 7-8B model fully on the Bifrost 9070 XT (~10GB usable) and is
    # 4x the old Modelfile default (2048) that truncated agent prompts. Per-tier
    # values in fleet_config override this; an explicit caller value overrides both.
    default_num_ctx: int = 8192

    # Timeouts (seconds)
    ollama_timeout: int = 270   # 30s below SUBTASK_WALL_CLOCK_TIMEOUT for clean error path
    # 2026-09-18 PER-TIER OLLAMA TIMEOUT. The flat 270 s cut gemma4 on Forge (T2) mid-generation:
    # max_tokens is stripped from local calls by the 2026-06-10 policy, so a long first turn
    # (9562 tokens, Lane A A32) ran past 270 s and came back as ReadTimeout -> 502 CASCADE_EXHAUSTED,
    # twice in a row. Keys are Tier values; a tier not listed keeps ollama_timeout. 600 stays under
    # the 720 s SUBTASK_WALL_CLOCK_TIMEOUT (autopilot_graph.py). Non-streaming path only: the
    # streaming paths' timeout is per chunk, so a long generation does not trip it.
    ollama_timeout_by_tier: dict[str, int] = {"2": 600}
    anthropic_timeout: int = 60
    gemini_timeout: int = 60
    groq_timeout: int = 30
    llamaserver_timeout: int = 270  # llama-server: model load can be slow on first request
    # NPU-TOOL-LEAF Phase 1: openai_compat reads `{provider}_timeout`. FLM on the XDNA2
    # NPU measured 1.23s ttft / 12.2 prefill / 10.6 decode tok/s on qwen3.5:4b (Gate 0
    # re-run, 2026-07-23) -- an order of magnitude below a GPU leaf, and a tool-schema
    # prompt is prefill-heavy. 60s (the openai_compat default) would time out on schema
    # injection alone; 180s is sized from the Gate-0 numbers, NOT guessed.
    flm_timeout: int = 180
    openrouter_timeout: int = 60     # v115: OpenRouter cloud cascade (symmetry with other cloud providers)

    # Two-pass strategy settings
    two_pass_local_timeout: int = 30                # max seconds for local draft
    # Review-lane controls. The local review lane reads both via getattr(..., False), so they
    # MUST exist here or that code silently behaves as though they are switched off.
    local_review_preferred: bool = True    # True routes review to Forge even when cloud is allowed
    allow_paid_escalation: bool = False    # False makes a paid provider unreachable
    # Fan-out candidates are generated NON-streamed and in full, so this is a whole-answer
    # budget, not a time-to-first-token budget like two_pass_local_timeout (30s).
    fanout_candidate_timeout: int = 120     # max seconds for ONE fan-out candidate
    two_pass_min_band: str = "COMPLEX"              # minimum band to trigger two-pass (MODERATE stays local-only)

    # Confidence gate â€” skip cloud review if local draft passes checks
    confidence_gate_enabled: bool = True
    confidence_min_response_ratio: float = 0.3      # draft tokens / prompt tokens â€” below this = suspiciously short
    confidence_min_tokens: int = 20                  # absolute minimum draft length
    confidence_truncation_penalty: bool = True       # flag drafts that end mid-code-block
    confidence_hedging_penalty: bool = True          # flag drafts with hedging language

    # Logging
    log_routing_decisions: bool = True

    # AUTOPILOT reviewer protocol
    review_tier_exclusion: bool = True  # reviewer tier must differ from generating tier

    # Arbiter force-lock duration
    arbiter_force_lock_seconds: int = 120  # replaces hardcoded 600s

    # SearXNG enrichment (Session F)
    searxng_url: str = resolve("search")
    search_enrichment_enabled: bool = os.getenv("BIFROST_SEARCH_ENRICHMENT", "false").strip().lower() in ("1", "true", "yes", "on")  # master switch -- F2 2026-08-30: DISABLED by default (incidental enrichment was injecting unrelated corpus docs); set BIFROST_SEARCH_ENRICHMENT=true to restore, no rebuild
    search_confidence_threshold: float = 0.72       # enrich if classifier confidence < this
    search_max_results: int = 3                     # top N results injected into context

    # bifrost-kb RAG (Session J)
    # bifrost-kb runs in Docker Compose on Hearth — not in k3d.
    # From inside the Router pod, reach via host IP + mapped port.
    bifrost_kb_url: str = resolve("kb")
    bifrost_kb_timeout: int = int(os.getenv("BIFROST_KB_TIMEOUT", "15"))  # fast fail — KB miss is non-fatal (v108: env-var overridable)
    bifrost_kb_min_chunks: int = 1       # min chunks to count as a KB hit
    bifrost_kb_project: str = "bifrost-self"  # KB project to query (2026-07-10 grounding fix: "default"=0 chunks; corpus is bifrost-self)

    # SEC-2: Scoped API keys (enforcement)
    # When bifrost_api_key_inference is set, /v1/chat/completions requires a valid key.
    # When bifrost_api_key_autopilot is set, /autopilot/submit requires a valid key.
    # Unset = open/home-lab mode (no enforcement).
    bifrost_api_key_inference: Optional[str] = os.getenv("BIFROST_API_KEY_INFERENCE") or None
    bifrost_api_key_autopilot: Optional[str] = os.getenv("BIFROST_API_KEY_AUTOPILOT") or None
    bifrost_api_key_ops: Optional[str] = os.getenv("BIFROST_API_KEY_OPS") or None

    # MCP-3A-BUILD v119 (2026-05-21): Docker MCP Gateway HTTP endpoint for the
    # Igor server-side tool-execution loop (igor_tool_loop.py). Unset = loop
    # capability gate _should_use_tool_loop in main.py returns False (passthrough).
    mcp_gateway_url: Optional[str] = os.getenv("MCP_GATEWAY_URL") or None
    mcp_gateway_token: Optional[str] = os.getenv("MCP_GATEWAY_TOKEN") or None

    # Daily cloud budget cap (USD). 0 = no cap.
    bifrost_daily_budget_usd: float = float(os.getenv("BIFROST_DAILY_BUDGET_USD", "0"))


# Singleton â€” loaded from env / config file at startup
settings = RouterSettings()
classifier_config = ClassifierConfig()






