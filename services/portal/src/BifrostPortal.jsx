import React, { useState, useEffect, useCallback, useRef } from "react";

import CommandSurface from "./CommandSurface";

import ArchitectFanoutSurface from "./ArchitectFanout";
// 2.2-b: the mode tiers from step 2.5. Until this import existed NOTHING referenced ModeTier.jsx,
// so vite tree-shook the whole module out and `Evaluator` appeared 0 times in the built bundle --
// 2.5 shipped in nothing. This is also the first time the file is compiled by a build.
import { useModeTier, ModeTierSelector, TierContext, useTier, atLeast, Gated, TierBadge } from "./ModeTier";

// Phase 1 (BRIEF-PORTAL-REDESIGN rev2): every colour now comes from ./theme/tokens (values unchanged).
import {
  BG, SURF, ELEV, BORD, BORDB, TX, TXD, TXM, GRN, BLU, AMB, ORG, RSE, PRP,
  PRESET_ROUTING, PRESET_AUTOPILOT, PRESET_PRIVACY, PRESET_RFP,
  TIER_1A_HEARTH, TIER_1A_INSTRUCT, TIER_2_QWEN_CODER, TIER_2_QWEN_KILO2,
  TIER_3_GEMINI, TIER_3_FAST, TIER_3_OPENROUTER,
  MODE_WORKSHOP_OFFLINE, SLATE, SLATE_LT,
  WARN_YEL, WARN_YEL_BG, WARN_YEL_BORDER, LOCAL_CYAN, ERR_RED, BLACK,
  TERM_MATRIX, TERM_GRN, TERM_GRN_GLOW, TERM_GRN_GLOW_STRONG,
  fs,
} from "./theme/tokens";

import {

  PieChart, Pie, Cell, ResponsiveContainer,

  AreaChart, Area, Tooltip,

} from "recharts";



// ???????????????????????????????????????????????????????????

// CONFIG ?" no mock data: every figure comes from a live endpoint (Phase 2 step 2.6)

// ???????????????????????????????????????????????????????????

const BROADCASTER = "/broadcaster";

const ARBITER     = "/arbiter";

const PROMETHEUS  = "/prom"

// -- psData model name helper (FIX 5) --
function getPsModel(psData, machine, port) {
  // v4.7-obs fix: psData now comes from ROUTER /fleet/ps with endpoints[] shape.
  // Each endpoint has machine + base_url; match on both.
  if (!psData?.endpoints) return null;
  const portStr = `:${port}`;
  const ep = psData.endpoints.find(e => e.machine === machine && e.base_url?.includes(portStr));
  if (!ep?.loaded_models?.length) return null;
  return findLoadedForTier(ep);
}

// -- A2: match a tier's loaded model instead of blindly taking loaded_models[0].
// On Forge, Ollama co-loads nomic-embed-text alongside serving models and often
// lists it first, which collapsed every Forge tier row to the embed model. With a
// configuredModel, match it exactly (null => "not loaded"); without one, prefer the
// first non-embedding model, falling back to [0] so pure-embed endpoints still resolve.
function findLoadedForTier(ep, configuredModel) {
  const loaded = ep?.loaded_models || [];
  if (!loaded.length) return null;
  const strip = m => m.replace(/:latest$/, "");
  if (configuredModel) {
    const want = strip(configuredModel);
    const hit = loaded.find(m => strip(m) === want);
    return hit ? strip(hit) : null; // null -> "not loaded"
  }
  const serving = loaded.find(m => !/embed/i.test(m));
  return strip(serving || loaded[0]);
}
; // OBSERVE_LIVE_PATCH

const POLL_MS     = 15_000;

const DEMO_PROMPTS = [
  { id: "routing1", label: "ROUTING 1", color: PRESET_ROUTING, strategy: "INTERACTIVE",
    text: "What is RAG? Explain in one sentence." },
  { id: "routing2", label: "ROUTING 2", color: PRESET_ROUTING, strategy: "INTERACTIVE",
    text: "Write a Python function that validates an email address using regex." },
  { id: "routing3", label: "ROUTING 3", color: PRESET_ROUTING, strategy: "INTERACTIVE",
    text: "Design a privacy-preserving AI inference architecture for insurance underwriting workflows. Include data flow, model tiers, and compliance considerations." },
  { id: "autopilot", label: "AUTOPILOT", color: PRESET_AUTOPILOT, strategy: "AUTOPILOT",
    text: "Build a Python REST API with authentication, database layer, business logic, and test suite. Use FastAPI and SQLite." },
  { id: "privacy", label: "PRIVACY", color: PRESET_PRIVACY, strategy: "INTERACTIVE",
    text: "Summarize the key privacy advantages of on-premise AI inference for insurance underwriting workflows. Focus on data sovereignty, regulatory compliance, and audit trail." },
  { id: "rfp", label: "RFP ENGINE", color: PRESET_RFP, strategy: "AUTOPILOT",
    text: "Write a complete RFP response for the KIPDA Employee Benefits Broker RFP. Cover executive summary, technical approach, team qualifications, past performance, and pricing narrative." },
];

const ROUTER      = "/router";

// demo-edit-1: model the Architect persona routes Converse through. This is a Router
// tool-loop model name (see demo-edit-4) -- the Router runs a server-side tool loop
// only for the aliases in _TOOL_LOOP_OPT_IN_MODELS, and since CEREMONY-92 only when
// the caller opts in with X-Tool-Loop (see the Architect fetch below).
//
// 2026-08-23: was "bifrost-coder-toolmaster". That alias CANNOT use tools -- its ollama
// TEMPLATE is `{{ .Prompt }}`, a raw passthrough that renders no roles, no .Tools and no
// .System, so tool definitions never reach the model and it echoed
// `<function=example_function_name>` boilerplate to the 4096-token cap (69.2s, zero tool
// calls). CEREMONY-93 dropped it from the router opt-in list. bifrost-igor-solo has a
// real tool template and was measured 4/4 clean at ~11s doing grep_repo / list_dir.
const ARCHITECT_MODEL = "bifrost-igor-solo";

// demo-edit-2: Compare mode dispatches ONE prompt to the tier ladder, by TIER (via
// X-Tier-Override), not by raw model tag. Each cell is fetched independently so a
// slow/failed cell degrades on its own and never breaks the panel. `prov` is the
// provenance label shown per cell (local vs cloud).
const COMPARE_TIERS = [
  { key: "t1g", label: "T1 - granite / Bifrost", tier: "1a-coder",  prov: "local" },
  { key: "t1m", label: "T1 - mistral / Hearth",  tier: "1a-hearth", prov: "local" },
  { key: "t2",  label: "T2 - Forge",             tier: "2",         prov: "local" },
  { key: "t3",  label: "T3 - cloud",             tier: "3-claude",  prov: "cloud" },
];
const COMPARE_TIMEOUT_MS = 60000;

const KB          = "/kb";
const INHOUSE     = "/retriever";  // Bifrost-side inhouse_pk retriever (C1.1)

const KB_SUPPORTED = [".pdf", ".docx", ".md", ".txt"];



// ???????????????????????????????????????????????????????????

// DESIGN TOKENS

// ???????????????????????????????????????????????????????????

// Palette moved to ./theme/tokens.js (Phase 1); imported above.



const BAND_C = { TRIVIAL: GRN, MODERATE: BLU, COMPLEX: ORG, FRONTIER: PRP };

const TIER_C = {
  "1a-hearth":          TIER_1A_HEARTH,
  "1a-coder":           GRN,
  "1a-instruct":        TIER_1A_INSTRUCT,
  "1b":                 BLU,
  "2":                  AMB,
  "2a_bifrost_mistral": AMB,
  "2.5":                ORG,
  "2_devstral":         ORG,
  "2_qwen_coder":       TIER_2_QWEN_CODER,
  "2_qwen_coder_kilo2": TIER_2_QWEN_KILO2,
  "3-claude":           RSE,
  "3-gemini":           TIER_3_GEMINI,
  "3-fast":             TIER_3_FAST,
  "3-openrouter":       TIER_3_OPENROUTER,
};

const MODE_C = {

  JARVIS: GRN, WORKSHOP: BLU, WORKSTATION: AMB,

  REMOTE: PRP, NOMAD: ORG,

  "CLOUD-ONLY": RSE, "WORKSHOP-OFFLINE": MODE_WORKSHOP_OFFLINE, "WORKSHOP_OFFLINE": MODE_WORKSHOP_OFFLINE,

};

// A5: chrome tier pills -> live state. Map each TIER_C key to its serving endpoint
// (machine + base_url port) so a pill lights from /fleet/ps reachability + loaded models.
// fleet_config active_model is a router ALIAS (not the served ollama tag), so we light by
// endpoint-has-non-embed-model rather than per-model-name matching. Cloud tiers (3-*) are
// not probeable from /fleet/ps -> rendered "dim" (configured, warmth unconfirmed).
const TIER_ENDPOINT = {
  "1a-hearth":   { machine: "hearth",  port: ":11434" },
  "1a-coder":    { machine: "bifrost", port: ":11434" },
  "1a-instruct": { machine: "bifrost", port: ":11434" },
  "1b":          { machine: "bifrost", port: ":11434" },
  "2":           { machine: "forge",   port: ":11434" },
  "2_qwen_coder_kilo2": { machine: "forge", port: ":11434" },
  "2.5":         { machine: "forge",   port: ":11435" },   // Igor moved off :11434 on 2026-10-01
  "3-Claude":    { cloud: true },
  "3-Gemini":    { cloud: true },
  "3-Fast":      { cloud: true },
};

const MODE_DESC = {

  JARVIS:              "All tiers available. Full local inference stack. Maximum parallelism.",

  WORKSHOP:            "Single-node mode. Bifrost profiles compensating. Forge offline.",

  WORKSTATION:         "Forge-primary. Bifrost 1b available. Full large-model capability.",

  REMOTE:              "Surface thin client via Tailscale. Full stack via tunnel.",

  NOMAD:               "Portable client via Tailscale. Full stack via tunnel. Cloud fallback active.",

  "CLOUD-ONLY":        "Emergency mode. Embeddings + cloud APIs only.",

  "WORKSHOP-OFFLINE":  "Privacy mode. Zero cloud egress. Local inference only.",

  "WORKSHOP_OFFLINE":  "Privacy mode. Zero cloud egress. Local inference only.",

};



// ???????????????????????????????????????????????????????????

// (mock data removed -- see the note below)

// ???????????????????????????????????????????????????????????

// Mock data REMOVED in Phase 2 step 2.6 (D5). M_STATUS (113 lines), M_METRICS (49 lines) and
// the `rng` seed helper they used are gone. They were not merely a dev convenience: both fetch
// functions returned them from their catch blocks, so an unreachable Router or a dead
// Prometheus rendered as plausible traffic, latency and cloud spend. Absent figures now reach
// the panels, which render NO_DATA. Recover from git if a fixture is ever wanted for tests.




// ???????????????????????????????????????????????????????????

// API

// ???????????????????????????????????????????????????????????

function deriveStatus(raw) {

  const sig = raw.signals || {};

  const sigBool = (k) => sig[k]?.value === "TRUE";

  const normalSignals = Object.fromEntries(

    Object.entries(sig).map(([k, v]) => [k, v?.value === "TRUE"])

  );

  const bifrostModels = raw.bifrost_loaded_models || [];

  const bCoder    = bifrostModels.find(m => m.includes("coder")) || bifrostModels[0] || "?";

  const bInstruct = bifrostModels.find(m => !m.includes("coder")) || "?";

  const forgeTiers = (raw.tiers || []).filter(t => t.machine === "Forge");

  const machines = {

    bifrost: {

      up:      sigBool("bifrost_ollama_live"),

      profile: raw.bifrost_profile || null,

      gpu: { label:"9070 XT", port:11434, model:bCoder,    vram_used:null, vram_total:14.5, tok_s:null },

      cpu: { label:"CPU",     port:11435, model:bInstruct, tok_s:null, role:"classifier" },

    },

    hearth: {

      up: sigBool("hearth_ollama_live"),

      primary: { port:11434, gpu:"RX 5700 XT",  model:"loading...", vram_used:null, vram_total:8.0,  tok_s:null },

      vega8:   { port:11436, gpu:"Vega 8 iGPU", model:"loading...",   gtt_used:null,  gtt_total:16.0, tok_s:null, embed_loaded:sigBool("hearth_embed_live") },

    },

    forge: {

      up:         sigBool("forge_lan_reachable"),

      profile:    raw.forge_profile || null,

      slots:      forgeTiers.map(t => ({ tier:t.tier, model:t.model, vram_used:0, tok_s:null, active:t.status==="available"||t.status==="healthy" })),

      vram_used:  (raw.forge_vram_used_bytes || 0) / 1e9,

      vram_total: 96.0,

    },

  };

  return { ...raw, signals: normalSignals, machines };

}



// portal-fix-v2 (2026-05-20): derive signals + machines from /router/fleet/ps + /router/status.
// Broadcaster (:8092) and Arbiter (:8082) are unreachable through k3d serverlb (host ports
// not exposed). Router already aggregates per-endpoint status via /fleet/ps. Pills + machine
// "up" indicators now reflect ACTUAL backend reachability instead of mock fallback or
// broadcaster-aggregated signals. Eliminates dependency on broadcaster + arbiter for the
// Observe surface.
function deriveStatusFromRouter(ps, st) {

  const eps = ps?.endpoints || [];

  const epByMachinePort = (machine, portStr) =>

    eps.find(e => e.machine === machine && e.base_url?.includes(portStr));

  const anyOnline = (machine) =>

    eps.some(e => e.machine === machine && e.status === "online");



  const bifrostGpu  = epByMachinePort("bifrost", ":11434");

  const hearthGpu   = epByMachinePort("hearth",  ":11434");

  const hearthVega8 = epByMachinePort("hearth",  ":11436");

  const forgeGpu    = epByMachinePort("forge",   ":11434");

  const forgeIgor   = epByMachinePort("forge",   ":11435");

  const forgeNpu    = epByMachinePort("forge",   ":8003");



  const bifrostLoaded = (bifrostGpu?.loaded_models || []);

  const bCoder    = bifrostLoaded.find(m => m.includes("coder")) || bifrostLoaded[0] || "?";

  const bInstruct = bifrostLoaded.find(m => !m.includes("coder")) || "?";



  // 2026-10-01: Forge still CO-LOADS T2 (evaluator) + T2.5 (Igor), but they are no longer on one
  // endpoint -- T2 is :11434 and Igor is :11435, two ollama instances on the one host. The split
  // exists because gemma4 on ROCm emits <unused24> when warm with flash attention on, so :11434
  // runs FLASH ATTENTION OFF (a signed arm) while Igor's instance runs it ON. Do NOT re-merge
  // these two lookups: collapsing them is what made this card read Igor's model off the T2
  // endpoint, find nothing there, and report "Igor (idle)" while Igor was loaded and serving.
  // Read EACH tier's model LIVE from ITS OWN endpoint; show 'idle' only when that endpoint has
  // no matching model loaded.
  const _forgeLoaded = (forgeGpu?.loaded_models || []);
  const _forgeFam = (re) => { const m = _forgeLoaded.find(x => re.test(x)); return m ? m.replace(/:latest$/, '') : null; };
  const _igorLoaded = (forgeIgor?.loaded_models || []);
  const _igorFam = (re) => { const m = _igorLoaded.find(x => re.test(x)); return m ? m.replace(/:latest$/, '') : null; };
  const _forgePort = parseInt((forgeGpu?.base_url  || '').match(/:(\d+)/)?.[1] || '11434', 10);
  const _igorPort  = parseInt((forgeIgor?.base_url || '').match(/:(\d+)/)?.[1] || '11435', 10);
  const _forgeUp = forgeGpu?.status === 'online';
  const _igorUp  = forgeIgor?.status === 'online';
  const _igorModel = _igorFam(/igor|gpt-oss/i);
  const forgeSlots = [
    { tier: '2',   model: _forgeFam(/gemma4|t2-gemma|mistral-small/i) || 'idle', machine: 'Forge',
      status: forgeGpu?.status, active: _forgeUp, port: _forgePort,
      vram_used: (forgeGpu?.size_vram || 0) / 1e9, tok_s: null },
    { tier: '2.5', model: _igorModel || 'Igor (idle)', machine: 'Forge',
      status: forgeIgor?.status, active: _igorUp && !!_igorModel, port: _igorPort,
      vram_used: (forgeIgor?.size_vram || 0) / 1e9, tok_s: null },
    ...(forgeNpu ? [{ tier: findLoadedForTier(forgeNpu) || 'npu', model: findLoadedForTier(forgeNpu) || '--',
      machine: 'Forge', status: forgeNpu.status, active: forgeNpu.status === 'online',
      port: parseInt((forgeNpu.base_url || '').match(/:(\d+)/)?.[1] || '8003', 10), vram_used: 0, tok_s: null }] : []),
  ];

  const forgeVram = forgeSlots.reduce((s, x) => s + (x.vram_used || 0), 0);



  return {

    mode: st?.mode || "JARVIS",

    confidence: 1.0,

    tiers: forgeSlots,

    bifrost_loaded_models: bifrostLoaded,

    bifrost_profile: st?.profile || null,

    forge_profile: null,

    forge_vram_used_bytes: forgeVram * 1e9,

    signals: {

      bifrost_ollama_live:  bifrostGpu?.status === "online",

      hearth_ollama_live:   hearthGpu?.status === "online",

      hearth_vega8_live:    hearthVega8?.status === "online",

      hearth_embed_live:    hearthVega8?.status === "online",

      forge_lan_reachable:  anyOnline("forge"),

      forge_ollama_live:    forgeGpu?.status === "online",

      api_available:        true,

    },

    machines: {

      bifrost: {

        up:      bifrostGpu?.status === "online",

        profile: st?.profile || null,

        gpu: {

          port: 11434, label: "9070 XT",

          model: bCoder,

          vram_used: bifrostGpu?.size_vram != null ? bifrostGpu.size_vram / 1e9 : null,

          vram_total: 14.5, tok_s: null,

        },

        cpu: {

          port: 11435, label: "CPU",

          model: bInstruct,

          tok_s: null, role: "classifier",

        },

      },

      hearth: {

        up: hearthGpu?.status === "online",

        primary: {

          port: 11434, gpu: "RX 5700 XT",

          model: findLoadedForTier(hearthGpu) || "--",

          vram_used: hearthGpu?.size_vram != null ? hearthGpu.size_vram / 1e9 : null,

          vram_total: 8.0, tok_s: null,

        },

        vega8: {

          port: 11436, gpu: "Vega 8 iGPU",

          model: (hearthVega8?.loaded_models || [])[0]?.replace(/:latest$/, "") || "nomic-embed-text",

          gtt_used: hearthVega8?.size_vram != null ? hearthVega8.size_vram / 1e9 : null,

          gtt_total: 16.0, tok_s: null,

          embed_loaded: hearthVega8?.status === "online",

        },

      },

      forge: {

        up:         anyOnline("forge"),

        profile:    null,

        slots:      forgeSlots,

        vram_used:  forgeVram,

        vram_total: 96.0,

      },

    },

  };

}



// Phase 2 step 2.6 (D5). A figure nobody measured must never render as a number.
//
// The M_STATUS / M_METRICS objects used to be this app's universal fallback, so every panel was
// guaranteed a number no matter what the Router did. With them gone an absent reading reaches the UI,
// and `null.toFixed()` throws -- which in React unmounts the whole tree rather than one tile. Hence
// these three helpers, used at every site that formats a possibly-absent figure.
//
// NO_DATA is the em dash LatencyPanel already uses (see its `fmt`, added in v4.7-obs because
// Prometheus returns NaN for histogram_quantile over an idle period -- the same lesson, learned once
// already). Written as an escape so this source line stays ASCII.
const NO_DATA = "\u2014";
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const fixed = (v, d) => (isNum(v) ? v.toFixed(d) : NO_DATA);

async function fetchStatus() {

  try {

    const [ps, st] = await Promise.all([

      fetch(`${ROUTER}/fleet/ps`).then(r => r.json()),

      fetch(`${ROUTER}/status`).then(r => r.json()),

    ]);

    return deriveStatusFromRouter(ps, st);

  } catch (e) {

    // 2.6 (D5): report no data rather than invent it. The object must keep the full SHAPE, because
    // ~30 sites read status.tiers / status.machines / status.signals and a missing nested key would
    // throw. The shape comes from deriveStatusFromRouter itself, called with empty inputs, so it
    // cannot drift from the real one; the inner catch covers that call failing too.
    console.warn("[fetchStatus] Router unreachable; reporting no data:", e);

    let shape;

    try { shape = deriveStatusFromRouter({}, {}); }

    catch { shape = { mode: null, tiers: [], signals: [], machines: {} }; }

    return { ...shape, unreachable: true, error: String(e?.message ?? e) };

  }

}



async function fetchArbiter() {

  // portal-fix-v2: Arbiter (:8082) unreachable through k3d serverlb. Return a stub so

  // the Observe Signals panel renders connected:false instead of stalling.

  return {

    connected:       false,

    confirmed_mode:  null,

    debouncing:      false,

    last_transition: null,

    broadcaster_connected: await fetch(`${BROADCASTER}/system/status`).then(r => r.ok ? r.json() : null).then(d => !!(d && d.observer_connected)).catch(() => false),
    reason:          "arbiter k3d port not exposed",

    history:         [],

  };

}



// fetchQuantum() removed 2026-07-11 (quantum-leg-removal): the /quantum feed and its
// QuantumRewardPanel are retired. The panel component below is dead (never rendered).

async function pq(query) {

  const url = `${PROMETHEUS}/api/v1/query?query=${encodeURIComponent(query)}`;

  const r = await fetch(url);

  const d = await r.json();

  return d.data?.result || [];

}



async function pqRange(query, start, end, step) {

  // Prometheus range query +' sparkline array [{t, v}]

  const url = `${PROMETHEUS}/api/v1/query_range?query=${encodeURIComponent(query)}&start=${start}&end=${end}&step=${step}`;

  try {

    const r = await fetch(url);

    const d = await r.json();

    const values = d.data?.result?.[0]?.values || [];

    return values.map(([t, v]) => ({ t, v: parseFloat(v) }));

  } catch { return []; }

}



async function fetchCost() {

  // Router /cost endpoint: { today, week, month, total, budget_day }

  try {

    const r = await fetch(`${ROUTER}/cost`);

    return await r.json();

  } catch { return null; }

}



async function fetchMetrics() {

  try {

    const now = Math.floor(Date.now() / 1000);

    const [bands, tiers, localPct, localTot, cloudTot, p50r, p90r, p99r, spark, cost, tokSRaw,
           fleetCfgRaw] =

      await Promise.allSettled([

        pq('bifrost_band_total'),

        pq('bifrost_tier_total'),

        pq('bifrost_local_percentage'),

        pq('bifrost_local_requests_total'),

        pq('bifrost_cloud_requests_total'),

        pq('histogram_quantile(0.5, rate(bifrost_request_latency_ms_bucket[5m]))'),

        pq('histogram_quantile(0.9, rate(bifrost_request_latency_ms_bucket[5m]))'),

        pq('histogram_quantile(0.99, rate(bifrost_request_latency_ms_bucket[5m]))'),

        pqRange('rate(bifrost_requests_total[1m])', now - 1800, now, 60),

        fetchCost(),

        pq('bifrost_tier_tok_s'),

        // fleet_config carries the AUTHORITATIVE tier -> base_url binding. It swallows its own
        // errors and resolves null, so it cannot fail this batch.
        fetchFleetConfig(),

      ]);



    const getVal = (r) => r.status === "fulfilled" ? parseFloat(r.value?.[0]?.value?.[1] || 0) : 0;

    const getLabeled = (r, label) =>

      r.status === "fulfilled"

        ? r.value.map(item => ({ k: item.metric[label], v: parseFloat(item.value[1]) }))

        : [];



    const sparkData = spark.status === "fulfilled" && spark.value?.length

      ? spark.value

      : [];



    const costData = cost.status === "fulfilled" && cost.value

      ? cost.value

      : null;



    return {

      bands:    getLabeled(bands, "band").map(x => ({ n: x.k.toUpperCase(), v: x.v })),

      tiers:    getLabeled(tiers, "tier"),

      localPct: getVal(localPct),

      local:    getVal(localTot),

      cloud:    getVal(cloudTot),

      p50:      Math.round(getVal(p50r)),

      p90:      Math.round(getVal(p90r)),

      p99:      Math.round(getVal(p99r)),

      sparkline: sparkData,

      // Brief Q Item 1: Router /cost returns *_usd suffixed keys + cap_day_usd for budget.
      // Brief P S1-1 fix: align Portal accessors with server-side schema.
      costDay:   costData?.today_usd   ?? costData?.today   ?? null,

      costWeek:  costData?.week_usd    ?? costData?.week    ?? null,

      costMonth: costData?.month_usd   ?? costData?.month   ?? null,

      budgetDay: costData?.cap_day_usd ?? costData?.budget_day ?? null,

      tokS: (() => {
        const raw = getLabeled(tokSRaw, "tier");
        const map = {};
        raw.forEach(({k, v}) => { map[k] = Math.round(v); });
        return map;
      })(),

      // tier -> base_url. Do NOT substitute /broadcaster/system/status: its tier table still
      // claims "1b = qwen2.5-coder:14b on Forge" while 1b is bound to Bifrost :11434.
      tierEndpoint: (() => {
        const cfg = fleetCfgRaw?.status === "fulfilled" ? fleetCfgRaw.value : null;
        const map = {};
        (cfg?.tiers || []).forEach(t => {
          if (t?.tier && t?.base_url) map[t.tier] = t.base_url;
        });
        return map;
      })(),

      // tier -> active_model, same source. Lets a card attribute an endpoint's resident models
      // to the tier that serves them instead of printing the endpoint total on every row.
      tierModel: (() => {
        const cfg = fleetCfgRaw?.status === "fulfilled" ? fleetCfgRaw.value : null;
        const map = {};
        (cfg?.tiers || []).forEach(t => {
          if (t?.tier && t?.active_model) map[t.tier] = t.active_model;
        });
        return map;
      })(),

    };

  } catch (e) {

    // 2.6 (D5): this catch was silent AND fabricating -- it swallowed the error and returned the
    // mock figures, so a dead Prometheus rendered as plausible traffic and cost. Now every figure
    // is absent and every collection empty; the panels render NO_DATA.
    console.warn("[fetchMetrics] metrics unreachable; reporting no data:", e);

    return {

      unreachable: true, error: String(e?.message ?? e),

      bands: [], tiers: [], localPct: null, local: null, cloud: null,

      p50: null, p90: null, p99: null, sparkline: [],

      costDay: null, costWeek: null, costMonth: null, budgetDay: null,

      tokS: {}, tierEndpoint: {}, tierModel: {},

    };

  }

}







// ???????????????????????????????????????????????????????????

// FETCH /api/ps ?" per-machine model + VRAM snapshot

// ???????????????????????????????????????????????????????????

async function fetchPs() {

  // v4.7-obs fix: was BROADCASTER /api/ps which returns library lists with null VRAM.
  // ROUTER /fleet/ps returns endpoints[] with live size_vram + correctly-loaded model per port.
  try {

    const r = await fetch(`${ROUTER}/fleet/ps`);

    return await r.json();

  } catch { return null; }

}



async function fetchFleetConfig() {

  try {

    const r = await fetch(`${ROUTER}/fleet/config`);

    return await r.json();

  } catch { return null; }

}



async function fetchFleetPs() {

  try {

    const r = await fetch(`${ROUTER}/fleet/ps`);

    return await r.json();

  } catch { return null; }

}



// ???????????????????????????????????????????????????????????

// FETCH TELEMETRY ?" Router SQLite events

// ???????????????????????????????????????????????????????????

async function fetchTelemetry() {

  try {

    const [bands, spend, recent] = await Promise.all([

      fetch(`${ROUTER}/telemetry/bands`).then(r => r.json()),

      fetch(`${ROUTER}/telemetry/cloud-spend`).then(r => r.json()),

      fetch(`${ROUTER}/telemetry/recent?n=10`).then(r => r.json()),

    ]);

    return {

      bands:       bands,                         // { TRIVIAL: N, MODERATE: N, ... }

      cloudSpend:  spend,                         // { total_usd, by_tier }

      recentEvents: recent.events || [],          // array of InferenceEvent rows

    };

  } catch { return { bands: {}, cloudSpend: { total_usd: 0, by_tier: {} }, recentEvents: [] }; }

}



// ───────────────────────────────────────────────────────────
// REAL-INFERENCE FLEET PROBE   (Brief: portal-observe-audit 2026-06-16)
// ───────────────────────────────────────────────────────────
// WHY: /fleet/ps reports status:"online" from METADATA only (GET /api/ps, /v1/models).
// A dead Ollama runner still answers /api/ps with 200, so the Router -- and therefore the
// Observe machine dots that mirror it -- paint the node GREEN even though it cannot emit a
// single token. That is exactly the 2026-06-16 incident: Bifrost's runner was dead ~14h
// while /api/tags stayed 200 the entire time, and Observe never went red.
//
// FIX (front-end, non-protected): issue a tiny REAL inference per LOCAL endpoint through the
// Router using X-Tier-Override, which pins a single-tier cascade with NO fallback
// (main.py:1694 `cascade = [_tier_override]`). A node is GREEN only if it actually completes;
// a 500 / runner-down / timeout shows RED even when /api/tags is 200. The embed node (:11436)
// is probed via /v1/embeddings (a real 768-dim vector or it's dead). Cloud (3-*) tiers are
// NEVER probed -- they cost money and this is a read-only monitor.
//
// CANONICAL HOME is the Router itself (one probe regardless of how many browser tabs are
// open). Until that ships, this runs client-side. See paste-pack:
//   v:\AI_Cloud\inhouse\pk\dispatch-journal\PASTEPACK-fleet-probe-backend.md   (TODO -- protected backend)

// PACKET-80 Option C (2026-08-21, John approved): the probe is SPLIT into two cadences.
//
//   every PROBE_MS             CHEAP liveness -- does this tier's runner answer at all?
//                              Reads /fleet/ps, whose Router-side implementation is a plain
//                              GET /api/ps per PHYSICAL endpoint (/v1/models as the FLM
//                              fallback). No generation, no model load, no GPU, no VRAM.
//   every CAPABILITY_PROBE_MS  the ORIGINAL generating probe, unchanged -- can this tier
//                              actually emit a token? Still X-Tier-Override pinned with no
//                              fallback, still a real /v1 completion. It is still the only
//                              thing that catches the 2026-06-16 "listed but not serving" lie.
//
// WHY (MEASURED 2026-08-21): the generating probe at 60s held bifrost-t2-gemma4-16k resident
// on Forge permanently -- Forge runs OLLAMA_KEEP_ALIVE=30m (measured; the old comment here
// claimed 5m and was simply wrong), so a 60s refresh never let it expire. Forge therefore
// reloaded a 17.5 GB model within ~6s of every unload_all() a bench harness performed, and
// "Forge was idle for this measurement" was never true for any bench ever run. Worse, the
// probe lives in page context, so whether a bench was contended depended on whether a
// browser tab happened to be open -- an unrecorded, uncontrolled variable in every fleet
// measurement. Measured baseline: 60 dispatches per tier per hour, 180/hr across the three
// chat tiers, sustained 15h unbroken on 2026-08-21.
//
// THE TRADE, stated here so it is not rediscovered later: between capability sweeps the
// cheap check CANNOT prove generation. A runner whose HTTP API answers but whose model
// cannot emit a token reads as "responding" for up to CAPABILITY_PROBE_MS. That is exactly
// the 2026-06-16 failure mode (Ollama listed models for ~14h while the runner was dead), so
// its detection latency moves from ~60s to <=60m. It is still DETECTED, not lost: a failed
// capability verdict forces alive=false even while the cheap check says "responding" -- see
// mergeProbe(). John accepted this trade explicitly when choosing Option C.
const PROBE_MS            = 60_000;       // cheap liveness sweep -- metadata only, no inference
const CAPABILITY_PROBE_MS = 60 * 60_000;  // generating sweep. MUST stay > Forge OLLAMA_KEEP_ALIVE

// G5 LOCAL TOKEN CLOCK cadence. Deliberately NOT POLL_MS: 15s is too slow for a number
// whose whole job is to visibly climb. This poll is metadata only -- it reads rows the
// router has already written and never causes generation.
const TOKEN_CLOCK_MS = 3_000;
const TOKEN_CLOCK_N  = 200;   // a ROW window, not a time window -- see the FLOOR marker
                                          // (30m). At 30m or below the model never expires and
                                          // this entire repair is void -- do not lower it without
                                          // re-checking OLLAMA_KEEP_ALIVE on Forge.
const LIVENESS_TIMEOUT_MS = 8_000;        // /fleet/ps is metadata; if it cannot answer in 8s the
                                          // Router is the problem, not any one tier.
const LIVENESS_FAIL_TOLERANCE = 3;        // consecutive /fleet/ps failures before the fleet is called
                                          // down. MEASURED 2026-08-21: /fleet/ps 502s roughly 1 call
                                          // in 5 through the portal proxy (pre-existing -- the
                                          // production portal log shows the same). The OLD probe
                                          // could not be hurt by this because every tier had its own
                                          // request; the cheap sweep has ONE, so a single blip would
                                          // otherwise flash every tier red at once. Hold the last
                                          // verdict until the failure is sustained (3 x PROBE_MS).
const PROBE_TIMEOUT_MS  = 25_000;  // MEASURED warm latencies (full generation, no max_tokens -- the
                                   // Router strips max_tokens->num_predict for locals): 1b ~6s,
                                   // 1a-hearth ~7s, Forge T2(30B) ~19s. 25s clears warm T2 + margin.
                                   // A cold model may RED for one cycle, then warms. (NPU is ~55s even
                                   // warm -> NOT live-probed; kind:"meta" below.)

// One canonical liveness target per PHYSICAL local endpoint.
// `tier` is the X-Tier-Override value -- a Tier ENUM value, not a model alias
// (main.py does `Tier(header)`; an alias would ValueError -> override silently ignored).
// `kind`: "chat" = /v1/chat/completions probe, "embed" = /v1/embeddings probe,
//         "meta" = NO real inference (too slow); reflect /fleet/ps metadata, labelled meta-only.
// Keep in sync with deriveStatusFromRouter ports.
const PROBE_TARGETS = [
  { key: "bifrost:gpu",    machine: "bifrost", slot: "gpu",     port: "11434", tier: "1b",           kind: "chat"  },
  { key: "hearth:primary", machine: "hearth",  slot: "primary", port: "11434", tier: "1a-hearth",    kind: "chat"  },
  { key: "hearth:vega8",   machine: "hearth",  slot: "vega8",   port: "11436", tier: "embed-hearth", kind: "embed" },
  { key: "forge:gpu",      machine: "forge",   slot: "gpu",     port: "11434", tier: "2",            kind: "chat"  },
  // Igor moved to its OWN ollama instance on :11435 (2026-10-01). "meta", not "chat", and
  // deliberately so: a chat probe here is a real inference against a 116.8B model, which would
  // wake a ~60 GB resident on every poll cycle. Igor is pinned, so /fleet/ps metadata is
  // sufficient to prove residency. Same reasoning as forge:npu below, different cause.
  { key: "forge:igor",     machine: "forge",   slot: "igor",    port: "11435", tier: "2.5",          kind: "meta"  },
  // NPU (FastFlowLM) measured ~55s even warm via the Router, and max_tokens can't be bounded
  // client-side -> a real-inference probe is impractical. Show metadata status, labelled meta-only,
  // rather than fake green or spam red. (Canonical backend probe CAN bound gen -- see paste-pack.)
  { key: "forge:npu",      machine: "forge",   slot: "npu",     port: "8003",  tier: "npu",          kind: "meta"  },
];

async function probeOne(t) {
  if (t.kind === "meta") {
    // No real inference for this target (see PROBE_TARGETS note). Liveness is taken from
    // /fleet/ps metadata in the panels; flag the result so consumers label it meta-only.
    return { key: t.key, alive: null, meta: true, ms: null, detail: "meta-only" };
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
  const t0 = Date.now();
  try {
    let resp, ok;
    if (t.kind === "embed") {
      // Embeddings always route to tier embed-hearth, so no override header. Do NOT name a
      // port here: that binding has moved twice and every literal written down went stale.
      // is needed. A non-empty embedding vector proves the runner actually executed.
      resp = await fetch(`${ROUTER}/v1/embeddings`, {
        method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: "bifrost-embed-hearth", input: "ping" }),
      });
      const j = resp.ok ? await resp.json() : null;
      ok = !!(resp.ok && j?.data?.[0]?.embedding?.length > 0);
    } else {
      // X-Tier-Override pins THIS tier with no fallback, so a dead runner surfaces as a
      // non-2xx instead of being masked by a healthy sibling tier. No max_tokens -- per
      // memory[ollama-no-num-predict] we never forward max_tokens -> num_predict on locals.
      resp = await fetch(`${ROUTER}/v1/chat/completions`, {
        method: "POST", signal: ctrl.signal,
        headers: { "Content-Type": "application/json", "X-Tier-Override": t.tier },
        body: JSON.stringify({
          model: "bifrost-auto",   // cosmetic: the override pins the tier regardless of model
          messages: [{ role: "user", content: "ping" }],
          temperature: 0, stream: false,
        }),
      });
      const j = resp.ok ? await resp.json() : null;
      const content = j?.choices?.[0]?.message?.content;
      ok = !!(resp.ok && typeof content === "string" && content.length > 0);
    }
    return { key: t.key, alive: ok, ms: Date.now() - t0, code: resp.status, detail: ok ? "serving" : `http ${resp.status}` };
  } catch (e) {
    const aborted = e?.name === "AbortError";
    return { key: t.key, alive: false, ms: Date.now() - t0, code: 0, detail: aborted ? "timeout" : "unreachable" };
  } finally {
    clearTimeout(timer);
  }
}

// PACKET-80: CHEAP liveness sweep. One GET /fleet/ps, mapped back onto PROBE_TARGETS by
// machine + port. /fleet/ps is pure metadata on the Router side (GET /api/ps per endpoint),
// so this loads no model, touches no GPU, and cannot re-pin a resident model.
//
// /api/ps semantics were chosen over /api/tags deliberately: /api/tags is a manifest-store
// directory listing that the HTTP server answers on its own, whereas /api/ps reflects the
// runner process table (which model is actually resident, and its VRAM). Neither proves
// generation -- that is what the capability sweep is for.
function endpointForTarget(fps, t) {
  const eps = (fps && fps.endpoints) || [];
  return eps.find(e => e.machine === t.machine && String(e.base_url || "").endsWith(":" + t.port)) || null;
}

async function runLivenessSweep() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), LIVENESS_TIMEOUT_MS);
  let fps = null;
  try {
    const r = await fetch(`${ROUTER}/fleet/ps`, { signal: ctrl.signal });
    fps = r.ok ? await r.json() : null;
  } catch { fps = null; } finally { clearTimeout(timer); }

  // null == "could not ask", which is NOT the same as "everything is down". The caller
  // decides whether that is a blip or an outage (see LIVENESS_FAIL_TOLERANCE).
  if (!fps) return null;

  const map = {};
  for (const t of PROBE_TARGETS) {
    if (t.kind === "meta") { map[t.key] = { up: null, meta: true, detail: "meta-only" }; continue; }
    const e = endpointForTarget(fps, t);
    if (!e)                         map[t.key] = { up: false, detail: "endpoint absent" };
    else if (e.status !== "online") map[t.key] = { up: false, detail: String(e.error || "offline").slice(0, 40) };
    else                            map[t.key] = { up: true,  detail: "responding" };
  }
  return map;
}

// Every non-meta target reported down because the Router itself cannot be reached.
function livenessAllUnreachable() {
  const map = {};
  for (const t of PROBE_TARGETS) {
    map[t.key] = t.kind === "meta"
      ? { up: null, meta: true, detail: "meta-only" }
      : { up: false, detail: "router unreachable" };
  }
  return map;
}

function probeAgeLabel(ts) {
  if (!ts) return "";
  const m = Math.floor((Date.now() - ts) / 60000);
  return m < 1 ? "just now" : `${m}m ago`;
}

// Fold the cheap verdict and the last capability verdict into the SAME map shape every
// existing consumer already reads ({ alive, ms, code, detail, meta }), so ProbeChip,
// LiveProbePanel and FanOutPanel need no rewiring. `alive` stays the single source of truth
// for every dot on the page.
function mergeProbe(live, cap) {
  const out = {};
  for (const t of PROBE_TARGETS) {
    const l = live && live[t.key];
    const c = cap && cap[t.key];
    if (t.kind === "meta" || (l && l.meta)) {
      out[t.key] = { key: t.key, alive: null, meta: true, ms: null, detail: "meta-only" };
      continue;
    }
    if (!l) { out[t.key] = undefined; continue; }   // nothing measured yet -> "probing..."
    // A FAILED capability verdict outranks a healthy cheap verdict. That exact combination
    // -- API responding, generation failing -- IS the listed-but-not-serving signature, and
    // it must go red rather than green.
    const capFailed = !!c && c.alive === false;
    const alive = !!l.up && !capFailed;
    let detail;
    if (!l.up)          detail = l.detail;
    else if (capFailed) detail = `gen fail: ${c.detail}`;
    else if (c)         detail = `responding, gen ok ${probeAgeLabel(c.at)}`;
    else                detail = "responding, gen unproven";
    out[t.key] = {
      key: t.key, alive, detail,
      ms: (c && c.ms != null) ? c.ms : null,
      code: c ? c.code : undefined,
      capAlive: c ? c.alive : null,
      capAt: c ? c.at : null,
    };
  }
  return out;
}

// Probe every local endpoint concurrently -> map keyed by PROBE_TARGETS.key.
// PACKET-80: this is now the CAPABILITY sweep, run every CAPABILITY_PROBE_MS, not every 60s.
// Its body is unchanged -- it is still a real X-Tier-Override pinned inference.
async function runFleetProbe() {
  const results = await Promise.all(PROBE_TARGETS.map(probeOne));
  const map = {};
  results.forEach(r => { map[r.key] = r; });
  return map;
}

// Compact real-probe status pill for a machine-card header. Surfaces what the /fleet/ps
// metadata status CANNOT: whether the runner actually completed an inference this cycle.
// metaUp(green) + probe-dead == the "listed but not serving" incident signature.
function ProbeChip({ probe, k, metaUp }) {
  const p = probe?.[k];
  let fg, bg, txt;
  if (!p)            { fg = TXD; bg = `${TXD}15`; txt = "probe…"; }
  else if (p.alive)  { fg = GRN; bg = `${GRN}15`; txt = p.capAlive ? `serving ${p.ms}ms` : "responding"; }
  else if (metaUp)   { fg = AMB; bg = `${AMB}18`; txt = `listed≠serving (${p.detail})`; }
  else               { fg = RSE; bg = `${RSE}18`; txt = `dead (${p.detail})`; }
  return (
    <div style={{
      fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7),
      color: fg, background: bg, border: `1px solid ${fg}40`,
      borderRadius: 3, padding: "1px 5px", whiteSpace: "nowrap",
    }}>{txt}</div>
  );
}



// ???????????????????????????????????????????????????????????

// PRIMITIVES

// ???????????????????????????????????????????????????????????

export function PulseDot({ alive, size = 8 }) {

  return (

    <span style={{

      display: "inline-block", width: size, height: size,

      borderRadius: "50%",

      background: alive ? GRN : RSE,

      boxShadow: alive ? `0 0 6px ${GRN}90` : `0 0 4px ${RSE}60`,

      animation: alive ? "blink 2.4s ease-in-out infinite" : "none",

      flexShrink: 0,

    }} />

  );

}



export function Panel({ children, style, span }) {

  return (

    <div style={{

      background: SURF,

      border: `1px solid ${BORD}`,

      borderRadius: 8,

      padding: "18px 20px",

      gridColumn: span || "auto",

      ...style,

    }}>

      {children}

    </div>

  );

}



export function Label({ children, style }) {

  return (

    <div style={{

      fontSize: fs(9), letterSpacing: "0.12em", textTransform: "uppercase",

      color: TXD, marginBottom: 12,

      fontFamily: "Instrument Sans, sans-serif", fontWeight: 600,

      ...style,

    }}>

      {children}

    </div>

  );

}



function BigNum({ value, unit, color, size = 34 }) {

  return (

    <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>

      <span style={{

        fontSize: fs(size), fontWeight: 600,

        fontFamily: "'JetBrains Mono', monospace",

        color: color || TX, lineHeight: 1,

      }}>{value}</span>

      {unit && <span style={{ fontSize: fs(12), color: TXD, fontFamily: "'JetBrains Mono', monospace" }}>{unit}</span>}

    </div>

  );

}



// 2026-09-29: SEG_COLORS (a cross-hue palette keyed on a hash of the model name) is gone.
// It could not satisfy "the meter matches the endpoint": on Hearth's BLU card a model could
// hash to GRN, and Forge's bar bore no relation to the AMB/TXM/PRP of its own tier rows.
// A segment now takes the colour of the endpoint that OWNS it, or a shade of the bar's colour.
// SEG_SHADE holds alpha suffixes -- full strength first, then progressively lighter. The steps
// are wide on purpose: the 2026-08-27 lesson was that two segments a viewer cannot tell apart
// are a bug, and shades of one hue start out harder to separate than distinct hues.
const SEG_SHADE = ["", "A6", "70", "4A", "30"];

function VramBar({ used, total, color = BLU, warnPct = 90, segments = null }) {

  // Split-colour segments (2026-08-27): one per resident model, largest first, so a card shows
  // WHICH models occupy the device rather than a single total. With fewer than two segments the
  // bar renders exactly as before -- so a single-model device is visually unchanged.
  const _segs = (segments || []).filter(s => s && s.bytes > 0).sort((a, b) => b.bytes - a.bytes);
  // Colour comes from the ENDPOINT, in two steps.
  //
  // 1. An explicit `color` on a segment is the colour the owning endpoint/tier is drawn with
  //    elsewhere on the card, so a band in the bar and its slot row agree. Forge supplies this by
  //    matching each resident model against the tier pins (the same rule slotVram uses).
  // 2. With no explicit colour -- several models on ONE endpoint, as on Bifrost and Hearth --
  //    the segment gets a SHADE of the bar's own colour. The old code reached for a different hue
  //    here, which is precisely what made the meter disagree with the endpoint.
  //
  // The 2026-08-27 guarantee is kept: assignment iterates segment names in LEXICAL order, not in
  // size order and not in /api/ps order, so a model keeps its shade when models change relative
  // size or the endpoint reorders them. Only a change in the SET of resident models moves one.
  const _shade = (hex, i) => {
    const h = String(hex || "");
    // Only a plain 6-digit hex can take an alpha suffix; a token that is anything else is left be.
    if (i <= 0 || !/^#[0-9a-fA-F]{6}$/.test(h)) return h;
    return h + SEG_SHADE[Math.min(i, SEG_SHADE.length - 1)];
  };
  const _segColorMap = (() => {
    const map = {};
    let step = 0;
    for (const s of [..._segs].sort((a, b) => String(a.name).localeCompare(String(b.name)))) {
      if (s.color) { map[s.name] = s.color; continue; }
      map[s.name] = _shade(color, step++);
    }
    return map;
  })();
  const _segColor = (s) => (s && s.color) || _segColorMap[s && s.name] || color;

  const rawPct = (used == null || !total) ? 0 : (used / total) * 100;

  const pct = Math.min(rawPct, 100);          // bar width caps at 100%

  const over = rawPct > 100;                  // A10: over-budget (e.g. Forge 97.9/96)

  const warn = rawPct >= warnPct;             // configurable warning threshold (default 90%)

  // One resident model: the solid bar should still be the OWNING endpoint's colour, not the
  // device's. Same rule as the segments. over/warn still win -- those are status, not identity.
  const _soleColor = (_segs.length === 1 && _segs[0].color) ? _segs[0].color : color;
  const barColor = over ? RSE : warn ? AMB : _soleColor;

  const headroom = (used != null && total) ? (total - used) : null;

  return (

    <div>

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>

        <span style={{ fontSize: fs(10), color: TXD, fontFamily: "'JetBrains Mono', monospace" }}>

          {used != null ? `${Number(used).toFixed(1)}GB` : "?"} / {total}GB{headroom != null ? ` · ${headroom.toFixed(1)}GB free` : ""}

        </span>

        <span style={{ fontSize: fs(10), color: over ? RSE : warn ? AMB : TXD, fontFamily: "'JetBrains Mono', monospace" }}>

          {rawPct.toFixed(0)}%{over ? " · over" : ""}

        </span>

      </div>

      <div style={{ height: 5, background: BORD, borderRadius: 3, overflow: "hidden", display: "flex" }}>

        {_segs.length > 1 ? _segs.map((s) => (

          <div key={s.name} title={`${s.name}  ${(s.bytes / 1e9).toFixed(2)}GB`} style={{

            height: "100%",

            width: `${total ? Math.min((s.bytes / 1e9 / total) * 100, 100) : 0}%`,

            background: _segColor(s),

            // 1px divider so same-hue shades stay legible; inset, so it costs no layout width.
            boxShadow: `inset -1px 0 0 ${BG}`,

            transition: "width 0.6s ease",

          }} />

        )) : (

          <div style={{

            height: "100%", width: `${pct}%`,

            background: barColor, borderRadius: 3,

            transition: "width 0.6s ease",

            boxShadow: `0 0 8px ${barColor}60`,

          }} />

        )}

      </div>

    </div>

  );

}



const CustomTooltip = ({ active, payload, label }) => {

  if (!active || !payload?.length) return null;

  return (

    <div style={{

      background: ELEV, border: `1px solid ${BORDB}`,

      borderRadius: 6, padding: "7px 11px",

    }}>

      <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(12), color: TX }}>

        {payload[0]?.value?.toFixed(2)}

      </div>

    </div>

  );

};



// ???????????????????????????????????????????????????????????

// TOP NAV

// ???????????????????????????????????????????????????????????

const OFFLINE_MODES = new Set(["WORKSHOP_OFFLINE", "JARVIS_OFFLINE"]);



// ============================================================================================
// SURFACE RAIL (2.2-b, D1) -- replaces the tab strip that used to live inside TopNav
// ============================================================================================
// 224 px expanded with labels, 56 px collapsed to icons, per rev4 2.2.
//
// The order of RAIL_ITEMS is load-bearing: rev4 requires the rail match the audited surface census
// exactly -- Converse, Observe, Fanout, Command, Maint -- and the strings must stay identical to the
// values `surface` is compared against in the shell, because that comparison is what mounts a surface.
//
// The icons are hand-authored stroke paths rather than a package. The project had no icon source at
// all (`<svg` occurred 0 times), and pulling one in would add an asset dependency to the same phase
// whose step 2.3 exists to remove the last one. They inherit colour from `currentColor`, so the active
// and inactive states need no separate icon variants.
//
// C9 (John 2026-10-13: "C9 tier list confirmed as drafted", with Maint at Operator so rev4 G1/G2
// holds). Evaluator sees Converse + Observe; Operator adds Fanout, Command and Maint. All five are
// present and function at Operator, as G1/G2 requires, and a lower tier only HIDES a surface (G2:
// nothing is removed). The per-control list is in coordination/pending
// OUTCOME-portal-a11y-contrast-tiers-and-phases-4-5-2026-10-13.md s.4. A tier is a VIEW filter,
// not security (ModeTier.jsx header). The selector sits at the foot of the rail because the tier is
// a property of the whole shell, not of any one surface.
const RAIL_ICON = { fill: "none", stroke: "currentColor", strokeWidth: 1.6,
                    strokeLinecap: "round", strokeLinejoin: "round" };

const RAIL_ITEMS = [
  { name: "Converse", icon: (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <path d="M3 5.5A1.5 1.5 0 0 1 4.5 4h9A1.5 1.5 0 0 1 15 5.5v5A1.5 1.5 0 0 1 13.5 12H7l-4 3Z" {...RAIL_ICON} />
    </svg>) },
  { name: "Observe", icon: (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <path d="M1.5 9S4 4.5 9 4.5 16.5 9 16.5 9 14 13.5 9 13.5 1.5 9 1.5 9Z" {...RAIL_ICON} />
      <circle cx="9" cy="9" r="2.25" {...RAIL_ICON} />
    </svg>) },
  { name: "Fanout", need: "Operator", icon: (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <path d="M4.5 9h3.5M8 9l3.5-4.5M8 9l3.5 4.5" {...RAIL_ICON} />
      <circle cx="3" cy="9" r="1.5" {...RAIL_ICON} />
      <circle cx="13.5" cy="4" r="1.5" {...RAIL_ICON} />
      <circle cx="13.5" cy="14" r="1.5" {...RAIL_ICON} />
    </svg>) },
  { name: "Command", need: "Operator", icon: (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <rect x="2.5" y="3.5" width="13" height="11" rx="1.5" {...RAIL_ICON} />
      <path d="M5.5 7.5 7.5 9.5 5.5 11.5M9.75 11.5h3" {...RAIL_ICON} />
    </svg>) },
  { name: "Maint", need: "Operator", icon: (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true">
      <path d="M3 5.5h12M3 9h12M3 12.5h12" {...RAIL_ICON} />
      <circle cx="6.5" cy="5.5" r="1.5" {...RAIL_ICON} />
      <circle cx="11.5" cy="9" r="1.5" {...RAIL_ICON} />
      <circle cx="7.5" cy="12.5" r="1.5" {...RAIL_ICON} />
    </svg>) },
];

// surface name -> the tier that shows it (undefined = every tier).
const SURFACE_NEED = Object.fromEntries(RAIL_ITEMS.map(r => [r.name, r.need]));

function SurfaceRail({ surface, setSurface, tier, setTier }) {
  // rev4 asks for "collapsible" and says nothing about persisting the state, so this resets to
  // expanded on reload rather than inventing a storage key.
  const [open, setOpen] = useState(true);
  return (
    <div style={{
      width: open ? 224 : 56, flexShrink: 0,
      background: SURF, borderRight: `1px solid ${BORD}`,
      display: "flex", flexDirection: "column",
      transition: "width 0.18s", overflow: "hidden",
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        title={open ? "Collapse rail" : "Expand rail"}
        aria-label={open ? "Collapse rail" : "Expand rail"}
        style={{
          display: "flex", alignItems: "center", gap: 10,
          height: 52, flexShrink: 0, padding: "0 19px",
          borderBottom: `1px solid ${BORD}`, color: TXM,
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9),
          letterSpacing: "0.08em",
        }}
      >
        <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" style={{ flexShrink: 0 }}>
          <path d="M3 5h12M3 9h12M3 13h12" {...RAIL_ICON} />
        </svg>
        {open && <span>MENU</span>}
      </button>

      <nav style={{ display: "flex", flexDirection: "column", gap: 2, padding: "8px 8px 0" }}>
        {RAIL_ITEMS.map(({ name, icon, need }) => {
          const on = surface === name;
          // C9 amendment (John 2026-10-14: "make below-tier items show greyed out with a tier
          // badge"). A surface above the current tier now STAYS in the rail, muted and inert, with
          // a badge naming the tier that shows it. The previous `.filter()` dropped the item
          // outright, so a hidden surface was indistinguishable from one that does not exist --
          // reported 2026-10-14 as "Portal appears to be missing surfaces (Fanout, Maint)". G2
          // still holds: nothing is removed, and a lower tier still cannot open the surface.
          //
          // `aria-disabled`, not `disabled`: a disabled button receives no mouse events, so its
          // title tooltip never fires -- and the tooltip is where the explanation lives. Keeping it
          // focusable also lets a keyboard viewer discover the surface and the tier it needs.
          // The badge sits UNDER the label, not beside it, because beside it does not fit and the
          // first build proved it: at 224 px the row leaves 142 px for label + badge, "Command"
          // alone measures 89.2 px, and the narrowest readable word badge measures 66.4 px -- five
          // candidate styles were measured in the rendered DOM and every one overflowed, clipping
          // Command by 28 px. On its own line the badge has 154 px for the 91.2 px it needs, so it
          // keeps full size and tracking rather than being abbreviated or truncated. Only locked
          // rows gain the second line. Collapsed (56 px) neither label nor badge renders, so the
          // muted icon plus the title carry it there.
          const locked = !atLeast(tier, need);
          return (
            <button
              key={name}
              onClick={() => { if (!locked) setSurface(name); }}
              title={locked ? `${name} -- shown at the ${need} detail level` : name}
              aria-current={on ? "page" : undefined}
              aria-disabled={locked || undefined}
              style={{
                display: "flex", alignItems: "center", gap: 12,
                padding: open ? "9px 11px" : "9px 0",
                justifyContent: open ? "flex-start" : "center",
                borderRadius: 6, width: "100%",
                background: on ? `${BLU}22` : "transparent",
                color: locked ? TXM : (on ? BLU : TXD),
                border: on ? `1px solid ${BLU}45` : "1px solid transparent",
                fontFamily: "Instrument Sans, sans-serif",
                fontSize: fs(12), fontWeight: 500,
                cursor: locked ? "default" : "pointer", transition: "all 0.18s",
              }}
            >
              <span style={{ display: "flex", flexShrink: 0, opacity: locked ? 0.6 : 1 }}>{icon}</span>
              {open && (
                <span style={{ display: "flex", flexDirection: "column",
                               alignItems: "flex-start", gap: 3, minWidth: 0 }}>
                  <span>{name}</span>
                  {locked && <TierBadge need={need} />}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div style={{
        marginTop: "auto", padding: open ? "10px 11px" : "10px 0",
        borderTop: `1px solid ${BORD}`,
        display: "flex", justifyContent: open ? "flex-start" : "center",
      }}>
        {open ? (
          <ModeTierSelector tier={tier} onChange={setTier} orientation="vertical" />
        ) : (
          <div
            title={`Mode tier: ${tier}`}
            style={{
              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10),
              color: TXM, border: `1px solid ${BORD}`, borderRadius: 4,
              width: 22, height: 22, display: "flex",
              alignItems: "center", justifyContent: "center",
            }}
          >{tier.slice(0, 1)}</div>
        )}
      </div>
    </div>
  );
}

function TopNav({ status, setStatus, psData, tierModel }) {

  const mode      = status?.mode || "?";

  const modeColor = MODE_C[mode] || TXD;

  const rawTiers  = status?.tiers || [];

  const isOffline = OFFLINE_MODES.has(mode);

  const [privacyLoading, setPrivacyLoading] = useState(false);
  // C9: switching the fleet mode is an Admin control. Below Admin the pill still SHOWS the mode.
  const canSwitchMode = atLeast(useTier(), "Admin");



  // Activate/deactivate privacy mode via Router /mode endpoint

  async function togglePrivacy() {

    if (privacyLoading || !canSwitchMode) return;

    setPrivacyLoading(true);

    const targetMode = isOffline ? "JARVIS" : "JARVIS_OFFLINE";

    // Optimistic update ?" flip mode immediately in UI

    if (setStatus && status) {

      setStatus(prev => ({ ...prev, mode: targetMode }));

    }

    // Activate/deactivate privacy mode via Arbiter /mode endpoint (bypasses debounce)

    try {

      await fetch(`${ARBITER}/mode`, {

        method: "POST",

        headers: { "Content-Type": "application/json" },

        body: JSON.stringify({ mode: targetMode }),

      });

      // Also update Router so cascade tables are correct immediately

      await fetch(`${ROUTER}/mode`, {

        method: "POST",

        headers: { "Content-Type": "application/json" },

        body: JSON.stringify({ mode: targetMode }),

      });

    } catch (e) {

      console.error("Privacy toggle failed:", e);

      // Revert on failure

      if (setStatus && status) setStatus(prev => ({ ...prev, mode: mode }));

    }

    setPrivacyLoading(false);

  }



  const activeTiers = new Set(

    rawTiers.filter(t => t.status !== "stub").map(t => t.tier)

  );

  // A5: live pill state from /fleet/ps. "on" = endpoint online with a non-embed model
  // loaded (warm); "dim" = configured but not loaded / offline / cloud (unprobeable);
  // "off" = endpoint absent from /fleet/ps. Falls back to status.tiers if unmapped.
  const tierLiveState = (tier) => {
    const map = TIER_ENDPOINT[tier];
    if (!map) return activeTiers.has(tier) ? "on" : "dim";
    if (map.cloud) return "dim";
    const ep = psData?.endpoints?.find(e => e.machine === map.machine && e.base_url?.includes(map.port));
    if (!ep) return "off";
    const want = (tierModel?.[tier] || "").replace(/:latest$/, ""); const warm = ep.status === "online" && (want ? !!findLoadedForTier(ep, want) : (ep.loaded_models || []).some(m => !/embed/i.test(m)));
    return warm ? "on" : "dim";
  };



  return (

    <div style={{

      height: 52, display: "flex", alignItems: "center",

      padding: "0 16px", borderBottom: `1px solid ${BORD}`,

      background: SURF, gap: 12, flexShrink: 0, zIndex: 10,

    }}>

      {/* Logotype */}

      <div style={{

        fontFamily: "'JetBrains Mono', monospace",

        fontWeight: 600, fontSize: fs(15), color: TX,

        letterSpacing: "0.06em", userSelect: "none",

      }}>

        <span style={{ color: modeColor, transition: "color 0.5s" }}>R</span>AMEAU

      </div>



      <div style={{ width: 1, height: 24, background: BORD }} />



      {/* Mode badge */}

      <div style={{

        display: "flex", alignItems: "center", gap: 7,

        background: `${modeColor}15`, border: `1px solid ${modeColor}40`,

        borderRadius: 20, padding: "4px 12px", transition: "all 0.5s",

      }}>

        <PulseDot alive={true} size={6} />

        <span style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10),

          color: modeColor, fontWeight: 500, letterSpacing: "0.07em",

        }}>{mode}</span>

      </div>



      {/* Tier strip */}

      <div style={{ display: "flex", gap: 4, flex: 1, justifyContent: "center", overflow: "hidden", minWidth: 0 }}>

        {Object.entries(TIER_C).map(([tier, color]) => {

          const liveState = tierLiveState(tier);

          const on = liveState === "on";

          return (

            <div key={tier} style={{

              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5),

              padding: "3px 7px", borderRadius: 4, flexShrink: 0,

              background: on ? `${color}18` : BORD,

              color: on ? color : TXM,

              border: `1px solid ${on ? color + "45" : BORD}`,

              letterSpacing: "0.04em", transition: "all 0.4s",

              opacity: on ? 1 : liveState === "off" ? 0.25 : 0.5,

            }}>{tier}</div>

          );

        })}

      </div>



      {/* Privacy toggle + NO CLOUD badge */}

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>

        {isOffline && (

          <div style={{

            fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),

            color: AMB, background: `${AMB}18`,

            border: `1px solid ${AMB}50`,

            borderRadius: 3, padding: "2px 8px",

            letterSpacing: "0.08em", fontWeight: 600,

            animation: "blink 2s ease-in-out infinite",

          }}>NO CLOUD</div>

        )}

        <button

          onClick={togglePrivacy}

          disabled={privacyLoading || !canSwitchMode}

          title={!canSwitchMode ? "Fleet mode. Switching it is shown at the Admin detail level." : isOffline ? "Disable privacy mode" : "Enable privacy mode (WORKSHOP-OFFLINE)"}

          style={{

            fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

            color: isOffline ? AMB : TXD,

            background: isOffline ? `${AMB}20` : `${BORD}`,

            border: `1px solid ${isOffline ? AMB + "60" : BORDB}`,

            borderRadius: 4, padding: "3px 9px",

            cursor: privacyLoading ? "wait" : canSwitchMode ? "pointer" : "default",

            transition: "all 0.3s",

            boxShadow: isOffline ? `0 0 8px ${AMB}30` : "none",

          }}

        >{privacyLoading ? "?" : isOffline ? "PRIVATE" : "HYBRID"}</button>

      </div>



      {/* 2.6 (D5): was a cosmetic LIVE/MOCK toggle -- it flipped a local flag, relabelled itself,
          and changed no data whatsoever. It is now a real indicator of whether the last poll
          reached the Router, which is what a viewer already believed it meant. Not clickable:
          there is nothing for a viewer to toggle. */}
      <div title={status?.unreachable
        ? `No data: ${status.error || "Router unreachable"}`
        : "Live data from the Router"} style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

        color: status?.unreachable ? RSE : GRN,

        background: status?.unreachable ? `${RSE}18` : `${GRN}18`,

        border: `1px solid ${status?.unreachable ? RSE : GRN}40`,

        borderRadius: 3, padding: "2px 6px",

        userSelect: "none",

      }}>{status?.unreachable ? "NO DATA" : "LIVE"}</div>




    </div>

  );

}



// ???????????????????????????????????????????????????????????

// MODE HERO

// ???????????????????????????????????????????????????????????

function ModeHero({ status }) {

  const mode      = status?.mode || "?";

  const sigs = Object.values(status?.signals || {});

  const conf = sigs.length ? sigs.filter(Boolean).length / sigs.length : 0;

  const modeColor = MODE_C[mode] || GRN;

  const tiers     = status?.tiers || [];

  const activeTierSet = new Set(tiers.filter(t => t.status !== "stub").map(t => t.tier));

  return (

    <Panel

      span="1 / -1"

      style={{

        background: `linear-gradient(140deg, ${SURF} 0%, ${modeColor}0A 100%)`,

        borderColor: `${modeColor}35`,

      }}

    >

      <div style={{ display: "flex", alignItems: "center", gap: 28 }}>

        {/* Mode name + desc */}

        <div style={{ minWidth: 220 }}>

          <div style={{

            fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif",

            textTransform: "uppercase", letterSpacing: "0.14em", marginBottom: 8,

          }}>Active Mode</div>

          <div style={{

            fontFamily: "'JetBrains Mono', monospace", fontWeight: 600,

            fontSize: fs(26), color: modeColor, letterSpacing: "0.04em",

            transition: "color 0.5s",

          }}>{mode}</div>

          <div style={{

            fontFamily: "Instrument Sans, sans-serif", fontSize: fs(11),

            color: TXD, marginTop: 5, lineHeight: 1.5, maxWidth: 340,

          }}>{MODE_DESC[mode] || ""}</div>

        </div>



        <div style={{ width: 1, height: 64, background: BORD }} />



        {/* Confidence */}

        <div style={{ textAlign: "center", minWidth: 80 }}>

          <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 8 }}>Confidence</div>

          <BigNum

            value={(conf * 100).toFixed(0)}

            unit="%"

            color={conf > 0.9 ? GRN : conf > 0.7 ? AMB : RSE}

            size={26}

          />

        </div>



        <div style={{ width: 1, height: 64, background: BORD }} />



        {/* A4/A9: duplicate "Available Tiers" pills removed (they mirror the chrome strip
            in TopNav). Reclaimed space shows a build readout. Router version/commit light
            up automatically once ROUTER-V adds `version`/`commit` to /status; "—" until then. */}

        <div style={{ flex: 1 }}>

          <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 10 }}>Build</div>

          <div style={{ display: "flex", flexDirection: "column", gap: 5, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5), color: TXD }}>

            <div>router <span style={{ color: TX }}>{status?.version || status?.router_version || "—"}</span>{(status?.commit || status?.git_sha) && (<span style={{ color: TXM }}> · {String(status.commit || status.git_sha).slice(0, 7)}</span>)}</div>

            <div>portal <span style={{ color: TX }}>v0.1</span></div>

            <div>mode <span style={{ color: TX }}>{status?.mode || "—"}</span> · strategy <span style={{ color: TX }}>{status?.strategy || "—"}</span></div>

          </div>

        </div>

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// MACHINE CARD

// ???????????????????????????????????????????????????????????

const MACHINE_META = {

  bifrost: { ip: "workstation.example.com", color: GRN,  role: "Primary Workstation" },

  hearth:  { ip: "server.example.com",  color: BLU,  role: "Always-On Server"    },

  forge:   { ip: "compute.example.com", color: ORG,  role: "Large Model Compute" },

};



function MachineCard({ name, data, signals }) {

  if (!data) return null;

  const meta = MACHINE_META[name];

  const mySignals = Object.entries(signals || {})

    .filter(([k]) => k.startsWith(name))

    .slice(0, 3);



  return (

    <Panel>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>

        <div>

          <div style={{

            fontFamily: "Instrument Sans, sans-serif", fontWeight: 600,

            fontSize: fs(13), color: meta.color, textTransform: "uppercase",

            letterSpacing: "0.07em",

          }}>{name}</div>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM, marginTop: 2 }}>

            {meta.ip}

          </div>

        </div>

        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>

          <PulseDot alive={data.up} />

          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXM }}>{meta.role}</div>

        </div>

      </div>



      {/* Model name */}

      <div style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD,

        marginBottom: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

        background: BORD, padding: "4px 8px", borderRadius: 4,

      }}>

        {data.model}

      </div>



      <VramBar used={data.vram_used} total={data.vram_total} color={meta.color} />



      <div style={{ marginTop: 14, display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>

        <div>

          <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 3 }}>Output</div>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(18), color: TX }}>

            {data.tok_s ?? "?"}<span style={{ fontSize: fs(9), color: TXD }}> tok/s</span>

          </div>

        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 5, alignItems: "flex-end" }}>

          {data.profile && (

            <div style={{

              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

              color: meta.color, background: `${meta.color}15`,

              border: `1px solid ${meta.color}40`, borderRadius: 3, padding: "2px 6px",

            }}>{data.profile}</div>

          )}

          {mySignals.map(([k, v]) => (

            <div key={k} style={{ display: "flex", alignItems: "center", gap: 5 }}>

              <span style={{ fontSize: fs(8.5), color: TXD, fontFamily: "Instrument Sans, sans-serif" }}>

                {k.replace(`${name}_`, "").replace(/_/g, " ")}

              </span>

              <PulseDot alive={v} size={5} />

            </div>

          ))}

        </div>

      </div>

    </Panel>

  );

}







// ???????????????????????????????????????????????????????????

// BIFROST CARD - GPU inference (routing classifier: rule-based in-router + NPU FLM on Forge :8003)

// ???????????????????????????????????????????????????????????

function BifrostCard({ data, signals, psData, tokS, probe, tierEp }) {

  if (!data) return null;

  // Probe-aware liveness: prefer the REAL-inference verdict for Bifrost's primary runner;
  // fall back to /fleet/ps metadata only until the first probe lands.
  const _bp = probe?.["bifrost:gpu"];
  const _dotAlive = _bp ? _bp.alive : data.up;

  const { gpu, cpu, profile } = data;

  const PROFILE_C = { "B-Light": GRN, "B-Dual": BLU, "B-Heavy": ORG };

  const profColor = PROFILE_C[profile] || GRN;

  const bifrostVram = (() => {
    // D9 COMPARISON (2026-10-16): match on machine, not on a host address. /fleet/ps emits
    // `machine` on every return path (main.py:4504-4600); the literal here was rewritten by
    // the genericise and this lookup then returned undefined, so the VRAM bar below fell back
    // to the broadcaster's stale gpu.vram_used and showed a plausible wrong figure. Same form
    // as _bifrostEp11434 seventeen lines down, which is the idiom in this file.
    const ep = psData?.endpoints?.find(e => e.machine === "bifrost" && e.base_url?.includes(":11434"));
    return ep?.size_vram != null ? ep.size_vram / 1e9 : null;
  })();

  // 2026-08-27 CORRECTION. The three lines that were here asserted that bifrost_tier_tok_s is
  // keyed BY MODEL NAME and "not by tier id". That is backwards, and it is why this card has
  // never shown a figure. Verified live against Prometheus: the metric carries a `tier` label
  // and the only series are {1a-hearth, 1b, 2, 2.5-solo}. Every model-name lookup missed.
  //
  // 2026-08-27 SECOND CORRECTION. The version here keyed on "1a-coder"/"1a-instruct" and I
  // reported the resulting blank as honest. The blank was real; the reason was wrong. Bifrost
  // does not serve 1a-coder at all -- fleet_config binds tier "1b" to http://127.0.0.1:11434
  // and telemetry maps granite-4.1-8b-tekken to "1b" on 15 of 15 events. That series EXISTS.
  // I trusted /broadcaster/system/status, whose tier table says 1b runs on Forge. It is stale.
  //
  // So: resolve by TIER BINDING, never by a tier name written here. Both times I hardcoded a
  // name I picked the wrong one, and the bindings move on their own.
  const _bifrostEp11434 = psData?.endpoints?.find(e => e.machine === "bifrost" && e.base_url?.includes(":11434"));
  const _bifrostBase = _bifrostEp11434?.base_url;
  const bifrostLoadedModelTokS = (() => {
    if (!_bifrostBase || !tierEp) return null;
    const tiers = Object.keys(tierEp).filter(t => tierEp[t] === _bifrostBase);
    for (const t of tiers) if (tokS?.[t] != null) return tokS[t];
    return null;
  })();



  return (

    <Panel>

      {/* Header */}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>

        <div>

          <div style={{

            fontFamily: "Instrument Sans, sans-serif", fontWeight: 600,

            fontSize: fs(13), color: GRN, textTransform: "uppercase", letterSpacing: "0.07em",

          }}>BIFROST</div>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM, marginTop: 2 }}>

            workstation.example.com

          </div>

        </div>

        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>

          <PulseDot alive={_dotAlive} />

          <Gated need="Admin"><ProbeChip probe={probe} k="bifrost:gpu" metaUp={data.up} /></Gated>

          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXM }}>Primary Workstation</div>

        </div>

      </div>



      {/* Profile badge */}

      {profile && (

        <div style={{ marginBottom: 10 }}>

          <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 5 }}>GPU Profile</div>

          <div style={{ display: "flex", gap: 5 }}>

            {["B-Light","B-Dual","B-Heavy"].map(p => (

              <div key={p} style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                padding: "2px 7px", borderRadius: 3,

                background: profile === p ? `${PROFILE_C[p]}18` : BORD,

                color: profile === p ? PROFILE_C[p] : TXM,

                border: `1px solid ${profile === p ? PROFILE_C[p] + "45" : BORD}`,

              }}>{p}</div>

            ))}

          </div>

        </div>

      )}



      {/* --- GPU instance --- */}

      <div style={{

        background: ELEV, borderRadius: 6, padding: "10px 12px", marginBottom: 8,

        border: `1px solid ${BORDB}`,

      }}>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>

            <div style={{ width: 5, height: 5, borderRadius: 1, background: GRN, flexShrink: 0 }} />

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: GRN }}>{gpu.label}</span>

          </div>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM }}>:{gpu.port}</span>

        </div>

        <div style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD,

          background: BORD, padding: "3px 7px", borderRadius: 3, marginBottom: 8,

          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

        }}>{getPsModel(psData,"bifrost","11434")||gpu.model}</div>

        <VramBar used={bifrostVram ?? gpu.vram_used} total={16} color={GRN}
                 segments={(_bifrostEp11434?.model_vram || []).map(m => ({ name: m.name, bytes: m.size_vram || m.size || 0 }))} />

        <div style={{ marginTop: 8, display: "flex", alignItems: "baseline", gap: 3 }}>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(16), color: TX }}>{bifrostLoadedModelTokS ?? "—"}</span>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD }}>tok/s</span>

        </div>

      </div>



      {/* CPU-classifier card removed (A3): :11435 stays in health polling but is no longer
          rendered as a Bifrost model card; the classifier role now lives on the Forge/NPU card. */}



      {/* Signals */}

      <div style={{ marginTop: 10, display: "flex", gap: 10, justifyContent: "flex-end" }}>

        {[["ollama live", signals?.bifrost_ollama_live]].map(([label, val]) => (

          <div key={label} style={{ display: "flex", alignItems: "center", gap: 4 }}>

            <span style={{ fontSize: fs(8.5), color: TXD, fontFamily: "Instrument Sans, sans-serif" }}>{label}</span>

            <PulseDot alive={val} size={5} />

          </div>

        ))}

      </div>

    </Panel>

  );

}





// ???????????????????????????????????????????????????????????

// FORGE CARD ?" profile slots (F-Multi / F-Max)

// ???????????????????????????????????????????????????????????

function ForgeCard({ data, signals, psData, tokS, probe, tierModel }) {
  // Per-slot residency (2026-08-27). Attribute an endpoint's resident models to the tier that
  // serves them, via fleet_config active_model, keyed off the slot's canonical tier (tokKeys[0]).
  // Before this every slot printed slot.ep.size_vram -- the ENDPOINT total -- so T2.5 and T2 each
  // read 82.4GB and the card claimed 164.8GB resident on a device with less than that.
  // Returns null when the model is not resident, and the caller then prints no figure at all.
  const slotVram = (slot) => {
    const want = tierModel?.[(slot.tokKeys || [])[0]];
    if (!want) return null;
    const base = String(want).split(":")[0];
    const hit = (slot.ep?.model_vram || []).find(m => String(m.name || "").split(":")[0] === base);
    return hit ? (hit.size_vram || hit.size || 0) : null;
  };
  // Resolve a slot's throughput by TIER id, first key that has a series. Returns undefined
  // when Prometheus has nothing for that tier, which the caller renders as no figure at all.
  const slotTokS = (slot) => (slot.tokKeys || []).map(k => tokS?.[k]).find(v => v != null);

  // 2026-09-29: the meter's segments take the colour of the tier that OWNS the model, by the
  // same active_model match slotVram uses, so a band in the bar and its slot row below are the
  // same colour. These three colours MUST stay equal to the slot table's below. A resident
  // model matching no tier pin returns null and falls back to a shade of the device colour,
  // which is honest: it is on the device, attributed to nothing.
  const _SEG_TIER_C = [["2.5-solo", AMB], ["2.5", AMB], ["2", TXM], ["npu", PRP]];
  const _segEndpointColor = (name) => {
    const base = String(name || "").split(":")[0];
    for (const [k, c] of _SEG_TIER_C) {
      const want = tierModel?.[k];
      if (want && String(want).split(":")[0] === base) return c;
    }
    return null;
  };

  if (!data) return null;

  // Probe-aware liveness for Forge's primary GPU runner (real inference, not metadata).
  const _fp = probe?.["forge:gpu"];
  const _dotAlive = _fp ? _fp.alive : data.up;

  // Brief Q Item 3: Broadcaster does not currently report forge_profile (returns null).
  // Pin F-Multi as the demo default until Arbiter runtime profile switching lands.
  const profile = data.profile || "F-Multi";

  const PROFILE_C = { "F-Multi": ORG, "F-Max": RSE };

  const profColor = PROFILE_C[profile] || ORG;

  // D9 COMPARISON (2026-10-16): the `|| base_url.includes(<literal>)` half was removed. It
  // was dead -- the genericise rewrote the literal -- and retaining it hid that the machine
  // branch is the only live one. Lowercase vocabulary: these are /fleet/ps endpoint objects,
  // NOT the broadcaster's raw.tiers, which uses capitalised machine names.
  const forgeEps   = psData?.endpoints?.filter(e => e.machine === "forge") || [];
  const gpuEp      = forgeEps.find(e => e.base_url?.includes(":11434"));
  // T2 is :11434 (gemma4, flash attention OFF -- a signed arm) and T2.5 is :11435
  // (Igor, flash attention ON). Igor moved to its own ollama instance on 2026-10-01.
  // 2026-10-07: BOTH rows of this card used to resolve :11434, so the T2.5 row read
  // Igor off the T2 endpoint and printed the wrong port. deriveStatusFromRouter was
  // fixed for this at the time and this component was not -- see the comment above
  // forgeSlots, which warns against exactly this merge. Keep the two lookups APART.
  const llamaEp    = forgeEps.find(e => e.base_url?.includes(":11434"));
  const igorEp     = forgeEps.find(e => e.base_url?.includes(":11435"));
  const npuEp      = forgeEps.find(e => e.base_url?.includes(":8003"));
  // Both instances, not just :11434. Measured 2026-10-07: gemma 18.79 GB on :11434
  // plus igor 64.32 GB on :11435 = 83.1 GB resident, which this bar used to draw as 19.
  const totalVram  = ((gpuEp?.size_vram ?? 0) + (igorEp?.size_vram ?? 0)) / 1e9;

  const _t25want = (tierModel?.["2.5-solo"] || tierModel?.["2.5"] || "").replace(/:latest$/, "");
  const gpuModels = _t25want
    ? (findLoadedForTier(igorEp, _t25want) || _t25want + " (not loaded)")
    : "--";
  const _t2want = (tierModel?.["2"] || "").replace(/:latest$/, "");
  const llamaModels = _t2want ? (findLoadedForTier(llamaEp, _t2want) || _t2want + " (not loaded)")
    : "--";
  const npuModels = npuEp?.loaded_models
    ?.map(m => m.replace(/:latest$/, ""))
    .join(", ") || "--";

  return (

    <Panel>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>
        <div>
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontWeight: 600, fontSize: fs(13), color: ORG, textTransform: "uppercase", letterSpacing: "0.07em" }}>FORGE</div>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM, marginTop: 2 }}>compute.example.com</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
          <PulseDot alive={_dotAlive} />
          <Gated need="Admin"><ProbeChip probe={probe} k="forge:gpu" metaUp={data.up} /></Gated>
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXM }}>Large Model Compute</div>
        </div>
      </div>

      {profile && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 5 }}>Active Profile</div>
          <div style={{ display: "flex", gap: 5 }}>
            {["F-Multi","F-Max"].map(p => (
              <div key={p} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), padding: "2px 7px", borderRadius: 3, background: profile === p ? `${PROFILE_C[p]}18` : BORD, color: profile === p ? PROFILE_C[p] : TXM, border: `1px solid ${profile === p ? PROFILE_C[p]+"45" : BORD}` }}>{p}</div>
            ))}
          </div>
        </div>
      )}

      {/* 2026-08-27: total was 96, which is not Forge's capacity. Forge is a Strix Halo APU
          (gfx1151 / STRXLGEN) with UNIFIED memory -- mem_info_vram_total reads 512MB because that
          is only the dedicated carve-out; models live in GTT. The addressable ceiling is GTT total,
          107.4GB (100GiB), which already excludes the system reservation. Re-derive with:
          cat /sys/class/drm/card0/device/mem_info_gtt_total   on Forge.
          At 96 the card read 82.4/96 = 86%; the honest figure is 82.4/107.4 = 77%. */}
      <VramBar used={totalVram > 0 ? totalVram : null} total={107.4} color={profColor}
               segments={[...(gpuEp?.model_vram || []), ...(igorEp?.model_vram || [])].map(m => ({ name: m.name, bytes: m.size_vram || m.size || 0, color: _segEndpointColor(m.name) }))} />

      <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 10 }}>
        {[
          // tokKeys: bifrost_tier_tok_s is keyed by TIER, not model name (see BifrostCard).
          // Prometheus currently exposes {1a-hearth, 1b, 2, 2.5-solo}; the tier table calls the
          // same tier "2.5", so both spellings are tried. NPU has no series and stays blank.
          { label: "T2",   color: TXM, model: llamaModels, ep: llamaEp, port: "11434", tokKeys: ["2"] },
          { label: "T2.5", color: AMB, model: gpuModels,   ep: igorEp,  port: "11435", tokKeys: ["2.5-solo", "2.5"] },
          { label: "NPU",  color: PRP, model: npuModels,   ep: npuEp,   port: "8003", role: "classifier", tokKeys: ["npu"] },
        ].map(slot => (
          <div key={slot.label} style={{ background: ELEV, borderRadius: 6, padding: "8px 12px", border: `1px solid ${slot.ep?.status === "online" ? BORDB : BORD}`, opacity: slot.ep?.status === "online" ? 1 : 0.45 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{ width: 5, height: 5, borderRadius: 1, background: slot.color, flexShrink: 0 }} />
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: slot.color }}>{slot.label}</span>
                {slot.role && (
                  <div style={{
                    fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: AMB,
                    background: `${AMB}15`, border: `1px solid ${AMB}40`,
                    borderRadius: 3, padding: "1px 5px",
                  }}>{slot.role}</div>
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {slotVram(slot) > 0 && (
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: AMB }}>{(slotVram(slot) / 1e9).toFixed(1)}GB</span>
                )}
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>:{slot.port}</span>
              </div>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5), color: slot.ep?.status === "online" ? TXD : TXM, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: "70%" }}>{slot.model}</div>
              {slot.ep?.status === "online" && slotTokS(slot) != null && (
                <div style={{ display: "flex", alignItems: "baseline", gap: 2 }}>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(14), color: TX }}>{slotTokS(slot)}</span>
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD }}>tok/s</span>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 10, display: "flex", gap: 10, justifyContent: "flex-end" }}>
        {/* v4.7-obs Fix 3: ollama-reachable indicator parallel to Bifrost/Hearth.
            Derived from psData since BROADCASTER does not expose forge_ollama_live. */}
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ fontSize: fs(8.5), color: TXD, fontFamily: "Instrument Sans, sans-serif" }}>ollama live</span>
          <PulseDot alive={gpuEp?.status === "online"} size={5} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <span style={{ fontSize: fs(8.5), color: TXD, fontFamily: "Instrument Sans, sans-serif" }}>lan reachable</span>
          <PulseDot alive={signals?.forge_lan_reachable} size={5} />
        </div>
      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// HEARTH CARD ?" dual GPU instances

// ???????????????????????????????????????????????????????????

function GttBar({ used, total }) {

  const pct   = Math.min((used / total) * 100, 100);

  const warn  = pct > 80;

  const color = warn ? AMB : SLATE;

  return (

    <div>

      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>

        <span style={{ fontSize: fs(10), color: TXD, fontFamily: "'JetBrains Mono', monospace" }}>

          {used != null ? `${used}GB` : "?"} / {total}GB GTT

        </span>

        <span style={{ fontSize: fs(10), color: warn ? AMB : TXD, fontFamily: "'JetBrains Mono', monospace" }}>

          {pct.toFixed(0)}%

        </span>

      </div>

      <div style={{ height: 5, background: BORD, borderRadius: 3, overflow: "hidden" }}>

        <div style={{

          height: "100%", width: `${pct}%`,

          background: `repeating-linear-gradient(

            60deg,

            ${color}CC,

            ${color}CC 4px,

            ${color}60 4px,

            ${color}60 8px

          )`,

          borderRadius: 3, transition: "width 0.6s ease",

        }} />

      </div>

    </div>

  );

}



function HearthCard({ data, signals, psData, tokS, probe, tierEp }) {

  if (!data) return null;

  const { primary, vega8 } = data;

  // Probe-aware liveness for Hearth's primary runner (:11434). The Vega8 embed runner
  // (:11436) has its own probe surfaced in the LiveProbePanel / vega8 slot below.
  const _hp = probe?.["hearth:primary"];
  const _dotAlive = _hp ? _hp.alive : data.up;

  const hearthVram = (() => {
    const ep = psData?.endpoints?.find(e => e.machine==="hearth" && e.base_url?.includes(":11434"));
    return ep?.size_vram != null ? ep.size_vram / 1e9 : null;
  })();
  const vega8Vram = (() => {
    const ep = psData?.endpoints?.find(e => e.machine==="hearth" && e.base_url?.includes(":11436"));
    return ep?.size_vram != null ? ep.size_vram / 1e9 : null;
  })();

  // 2026-10-07: the Nomic Embed row was REMOVED from this card. Tier embed-hearth is
  // bound to the Hearth dGPU :11434 -- the same endpoint as the primary row above it --
  // so the card showed one device twice, the second time labelled as an embed node. The
  // Vega 8 row already carries the 'embed loaded' chip. If an embed row comes back, keep
  // the lesson that paid for the old one: resolve the port through tierEp, NEVER a
  // literal. Embed has moved three times (:11435 -> Vega 8 :11436 -> dGPU :11434) and a
  // hardcoded port silently broke the row twice.


  // relevant signals

  const sigMap = {

    "ollama live":  signals?.hearth_ollama_live,

    "vega8 live":   signals?.hearth_embed_live,   // proxy: embed served by Vega 8

    "embed live":   signals?.hearth_embed_live,

  };



  return (

    <Panel>

      {/* Header */}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>

        <div>

          <div style={{

            fontFamily: "Instrument Sans, sans-serif", fontWeight: 600,

            fontSize: fs(13), color: BLU, textTransform: "uppercase", letterSpacing: "0.07em",

          }}>HEARTH</div>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM, marginTop: 2 }}>

            server.example.com

          </div>

        </div>

        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>

          <PulseDot alive={_dotAlive} />

          <Gated need="Admin"><ProbeChip probe={probe} k="hearth:primary" metaUp={data.up} /></Gated>

          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXM }}>

            Always-On Server

          </div>

        </div>

      </div>



      {/* --- Primary: RX 5700 XT --- */}

      <div style={{

        background: ELEV, borderRadius: 6, padding: "10px 12px", marginBottom: 8,

        border: `1px solid ${BORDB}`,

      }}>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>

            <div style={{ width: 5, height: 5, borderRadius: 1, background: BLU, flexShrink: 0 }} />

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: BLU }}>

              {primary.gpu}

            </span>

          </div>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM }}>

            :{primary.port}

          </span>

        </div>

        <div style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD,

          background: BORD, padding: "3px 7px", borderRadius: 3, marginBottom: 8,

          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

        }}>

          {getPsModel(psData,"hearth","11434")||primary.model}

        </div>

        <VramBar used={hearthVram ?? primary.vram_used} total={8} color={BLU}
                 segments={(psData?.endpoints?.find(e => e.machine==="hearth" && e.base_url?.includes(":11434"))?.model_vram || []).map(m => ({ name: m.name, bytes: m.size_vram || m.size || 0 }))} />

        <div style={{ marginTop: 8, display: "flex", alignItems: "baseline", gap: 3 }}>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(16), color: TX }}>

            {tokS?.["mistral:7b"] ?? tokS?.["mistral:latest"] ?? tokS?.["1a-hearth"] ?? "—"}

          </span>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD }}>tok/s</span>

        </div>

      </div>



      {/* --- Vega 8 iGPU --- */}

      <div style={{

        background: ELEV, borderRadius: 6, padding: "10px 12px",

        border: `1px solid ${BORDB}`,

      }}>

        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>

            <div style={{ width: 5, height: 5, borderRadius: 1, background: SLATE, flexShrink: 0 }} />

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: SLATE_LT }}>

              {vega8.gpu}

            </span>

          </div>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM }}>

            :{vega8.port}

          </span>

        </div>

        <div style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD,

          background: BORD, padding: "3px 7px", borderRadius: 3, marginBottom: 8,

          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

        }}>

          {getPsModel(psData,"hearth","11436")||vega8.model}

        </div>

        <VramBar used={vega8Vram ?? vega8.gtt_used} total={16} color={BLU} />

        <div style={{ marginTop: 8, display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>

          <div style={{ display: "flex", alignItems: "baseline", gap: 3 }}>

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(16), color: TX }}>

              {tokS?.["1a-overflow"] ?? "—"}

            </span>

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD }}>tok/s</span>

          </div>

          {vega8.embed_loaded && (

            <div style={{

              fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: GRN,

              background: `${GRN}15`, border: `1px solid ${GRN}40`,

              borderRadius: 3, padding: "2px 6px",

            }}>embed loaded</div>

          )}

        </div>

      </div>



      {/* Signals */}

      <div style={{ marginTop: 10, display: "flex", gap: 12, justifyContent: "flex-end" }}>

        {Object.entries(sigMap).map(([label, val]) => (

          <div key={label} style={{ display: "flex", alignItems: "center", gap: 4 }}>

            <span style={{ fontSize: fs(8.5), color: TXD, fontFamily: "Instrument Sans, sans-serif" }}>{label}</span>

            <PulseDot alive={val} size={5} />

          </div>

        ))}

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// BAND DONUT

// ???????????????????????????????????????????????????????????

function BandDonut({ bands }) {

  const total = bands.reduce((s, b) => s + b.v, 0);

  const DonutTooltip = ({ active, payload }) => {

    if (!active || !payload?.length) return null;

    return (

      <div style={{ background: ELEV, border: `1px solid ${BORDB}`, borderRadius: 6, padding: "7px 11px" }}>

        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: BAND_C[payload[0].name] }}>{payload[0].name}</div>

        <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(14), color: TX }}>{payload[0].value.toLocaleString()}</div>

      </div>

    );

  };



  return (

    <Panel>

      <Label>Complexity Distribution · since restart</Label>

      <div style={{ display: "flex", gap: 16, alignItems: "center" }}>

        <div style={{ flexShrink: 0 }}>

          <ResponsiveContainer width={110} height={110}>

            <PieChart>

              <Pie

                data={bands.map(b => ({ name: b.n, value: b.v }))}

                cx="50%" cy="50%"

                innerRadius={32} outerRadius={52}

                dataKey="value" strokeWidth={0} paddingAngle={2} isAnimationActive={false}

              >

                {bands.map(b => <Cell key={b.n} fill={BAND_C[b.n]} />)}

              </Pie>

              <Tooltip content={<DonutTooltip />} />

            </PieChart>

          </ResponsiveContainer>

        </div>

        <div style={{ flex: 1 }}>

          {bands.map(b => (

            <div key={b.n} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 7 }}>

              <div style={{ display: "flex", alignItems: "center", gap: 7 }}>

                <div style={{ width: 8, height: 8, borderRadius: 2, background: BAND_C[b.n], flexShrink: 0 }} />

                <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(11), color: TXD }}>{b.n}</span>

              </div>

              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>

                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(11), color: TX }}>{b.v.toLocaleString()}</span>

                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM, minWidth: 30, textAlign: "right" }}>

                  {((b.v / total) * 100).toFixed(0)}%

                </span>

              </div>

            </div>

          ))}

          <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${BORD}`, display: "flex", justifyContent: "space-between" }}>

            <span style={{ fontSize: fs(9), color: TXM, fontFamily: "Instrument Sans, sans-serif" }}>Total requests</span>

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(12), color: TX }}>{total.toLocaleString()}</span>

          </div>

        </div>

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// TIER BARS

// ???????????????????????????????????????????????????????????

function TierBars({ tiers }) {

  const max = Math.max(...tiers.map(t => t.v), 1);

  return (

    <Panel>

      <Label>Tier Hit Rates · since restart</Label>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>

        {tiers.map(({ k, v }) => (

          <div key={k} style={{ display: "flex", alignItems: "center", gap: 8 }}>

            <div style={{

              width: 72, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9),

              color: TIER_C[k] || TXD, textAlign: "right", flexShrink: 0,

            }}>{k}</div>

            <div style={{ flex: 1, height: 13, background: BORD, borderRadius: 3, overflow: "hidden" }}>

              <div style={{

                height: "100%", width: `${(v / max) * 100}%`,

                background: TIER_C[k] || BLU, borderRadius: 3,

                transition: "width 0.7s ease",

                opacity: 0.82,

              }} />

            </div>

            <div style={{ width: 38, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD, textAlign: "right" }}>

              {v.toLocaleString()}

            </div>

          </div>

        ))}

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// LOCAL / CLOUD SPLIT

// ???????????????????????????????????????????????????????????

// ============================================================
// AIR-GAP CONTROL
// ============================================================
// Restored to source 2026-08-17 from the served bundle index-BZ4ihzT5.js, which was its only
// copy. Contract confirmed against the live router (v358). The latch is enforced router-side
// (main.py _AIRGAP_LATCH): once air-gapped, it cannot be cleared over the network -- only by
// restarting the router. So once airgap is true this control disables itself rather than
// offering an undo it cannot deliver.
function useAirgap() {

  const [airgap, setAirgap] = useState(null);

  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {

    try {

      const r = await fetch(`${ROUTER}/airgap`);

      if (!r.ok) throw new Error(r.status);

      setAirgap((await r.json()).air_gapped);

    } catch { setAirgap(null); }

  }, []);

  useEffect(() => {

    refresh();

    const id = setInterval(refresh, POLL_MS);

    return () => clearInterval(id);

  }, [refresh]);

  const flip = useCallback(async (next) => {

    setBusy(true);

    try {

      const r = await fetch(`${ROUTER}/airgap`, {

        method: "POST",

        headers: { "Content-Type": "application/json" },

        body: JSON.stringify({ air_gapped: next }),

      });

      const d = await r.json();

      setAirgap(r.ok ? d.current : (d.air_gapped ?? null));

    } catch { setAirgap(null); } finally { setBusy(false); }

  }, []);

  return { airgap, busy, flip };

}

// null = router unreachable, so the state is UNKNOWN -- deliberately not shown as "hybrid",
// which would understate egress risk on a dead link.
function AirgapBadge({ ag }) {

  const { airgap, busy, flip } = ag;
  // C9: the badge is read-only below Admin; flipping egress posture is an Admin control.
  const canFlip = atLeast(useTier(), "Admin");

  const c = airgap === null ? TXD : airgap ? GRN : AMB;

  const label = airgap === null ? "LINK DOWN" : airgap ? "NO EGRESS" : "HYBRID";

  const latched = airgap === true;

  const locked = airgap === null || busy || latched || !canFlip;

  const title = airgap === null

    ? "router unreachable -- state unknown"

    : airgap

      ? "air-gapped: no cloud tier is in the cascade. Latched -- clearing it needs a router restart."

      : canFlip
        ? "hybrid: cloud tiers are in the cascade. Click to air-gap (cannot be undone from here)."
        : "hybrid: cloud tiers are in the cascade. Air-gapping is shown at the Admin detail level.";

  return (

    <div

      onClick={() => !locked && flip(true)}

      title={title}

      style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: c,

        background: `${c}18`, border: `1px solid ${c}40`, borderRadius: 3,

        padding: "2px 6px", userSelect: "none",

        cursor: locked ? "not-allowed" : "pointer",

        opacity: busy ? 0.5 : 1, transition: "all 0.2s",

      }}

    >

      {latched ? `${label} \u00b7 LOCKED` : label}

    </div>

  );

}

function LocalCloudPanel({ localPct, local, cloud, ag }) {

  const airgapped = ag?.airgap === true;

  // 2.6: an absent localPct must not read as 'below 70% local', which is the alarming end.
  const gc = !isNum(localPct) ? TXD : localPct > 85 ? GRN : localPct > 70 ? AMB : RSE;

  return (

    <Panel style={airgapped ? { borderColor: `${GRN}55` } : undefined}>

      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>

        <Label>Local / Cloud Split</Label>

        <div style={{ flex: 1 }} />

        {ag && <AirgapBadge ag={ag} />}

      </div>

      <div style={{ textAlign: "center", padding: "6px 0 10px" }}>

        <BigNum value={fixed(localPct, 1)} unit="%" color={gc} size={32} />

        <div style={{ fontSize: fs(9.5), color: TXD, fontFamily: "Instrument Sans, sans-serif", marginTop: 4 }}>

          local inference

        </div>

      </div>

      <div style={{ height: 7, background: BORD, borderRadius: 4, overflow: "hidden", marginBottom: 10 }}>

        <div style={{

          height: "100%", width: `${localPct}%`,

          background: `linear-gradient(90deg, ${GRN}, ${gc})`,

          borderRadius: 4, transition: "width 0.6s ease",

        }} />

      </div>

      <div style={{ display: "flex", justifyContent: "space-around" }}>

        {[[local, "local", GRN], [cloud, "cloud", RSE]].map(([n, lbl, c]) => (

          <div key={lbl} style={{ textAlign: "center" }}>

            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(15), color: c }}>{n.toLocaleString()}</div>

            <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em" }}>{lbl}</div>

          </div>

        ))}

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// LATENCY

// ???????????????????????????????????????????????????????????

function LatencyPanel({ p50, p90, p99 }) {

  // v4.7-obs Fix 4: NaN-safe formatter. Prometheus returns NaN for histogram_quantile when
  // there are no recent rate samples (idle period). Render em-dash placeholder instead of "NaNs".
  const fmt = ms => {
    if (ms == null || Number.isNaN(ms)) return "—";
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  };

  const rows = [["p50", p50, GRN], ["p90", p90, AMB], ["p99", p99, RSE]];

  return (

    <Panel>

      <Label>Request Latency</Label>

      <div style={{ display: "flex", flexDirection: "column", gap: 13 }}>

        {rows.map(([pct, ms, color]) => (

          <div key={pct} style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD }}>{pct}</span>

            <div style={{ flex: 1, height: 1, background: BORD, margin: "0 12px", opacity: 0.6 }} />

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(18), color, fontWeight: 500 }}>

              {fmt(ms)}

            </span>

          </div>

        ))}

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// THROUGHPUT SPARKLINE

// ???????????????????????????????????????????????????????????

function ThroughputPanel({ sparkline }) {

  // 2.6: `|| 0` reported a measured 0 req/s for an absent series. Absent is not zero.
  const cur = Array.isArray(sparkline) && sparkline.length
    ? sparkline[sparkline.length - 1]?.v
    : null;

  return (

    <Panel>

      <Label>Throughput</Label>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end", marginBottom: 10 }}>

        <BigNum value={fixed(cur, 2)} unit="req/s" color={BLU} size={28} />

        <span style={{ fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace" }}>rate(5m)</span>

      </div>

      <ResponsiveContainer width="100%" height={52}>

        <AreaChart data={sparkline} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>

          <defs>

            <linearGradient id="spk" x1="0" y1="0" x2="0" y2="1">

              <stop offset="5%"  stopColor={BLU} stopOpacity={0.28} />

              <stop offset="95%" stopColor={BLU} stopOpacity={0} />

            </linearGradient>

          </defs>

          <Tooltip content={<CustomTooltip />} />

          <Area type="monotone" dataKey="v" stroke={BLU} strokeWidth={1.5} fill="url(#spk)" dot={false} isAnimationActive={false} />

        </AreaChart>

      </ResponsiveContainer>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// COST TRACKER

// ???????????????????????????????????????????????????????????

function CostPanel({ costDay, costWeek, costMonth, budgetDay }) {

  // 2.6: NaN would make both `pct < 50` and `pct < 80` false and turn the tile RED, reading as a
  // blown budget when the truth is that nothing was measured. null is checked explicitly instead.
  const pct   = isNum(costDay) && isNum(budgetDay) && budgetDay > 0
    ? Math.min((costDay / budgetDay) * 100, 100)
    : null;

  const color = pct == null ? TXD : pct < 50 ? GRN : pct < 80 ? AMB : RSE;

  return (

    <Panel>

      <Label>Cloud Spend</Label>

      <div style={{ display: "flex", gap: 10, marginBottom: 14 }}>

        {[["Today", costDay, color, true], ["Week", costWeek, TXD, false], ["Month", costMonth, TXD, false]].map(

          ([lbl, val, c, big]) => (

            <div key={lbl} style={{ flex: 1, textAlign: "center" }}>

              <div style={{

                fontFamily: "'JetBrains Mono', monospace",

                fontSize: fs(big ? 22 : 14), color: c, fontWeight: big ? 600 : 400,

              }}>{isNum(val) ? `$${val.toFixed(2)}` : NO_DATA}</div>

              <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.08em", marginTop: 2 }}>{lbl}</div>

            </div>

          )

        )}

      </div>

      <div>

        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 5 }}>

          <span style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif" }}>Daily budget</span>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color }}>{pct == null ? NO_DATA : `${pct.toFixed(0)}%`}</span>

        </div>

        <div style={{ height: 5, background: BORD, borderRadius: 3, overflow: "hidden" }}>

          <div style={{

            height: "100%", width: `${pct == null ? 0 : pct}%`,

            background: color, borderRadius: 3, transition: "width 0.6s ease",

          }} />

        </div>

        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 4 }}>

          <span style={{ fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace" }}>$0</span>

          <span style={{ fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace" }}>{isNum(budgetDay) ? `$${budgetDay}/day cap` : `${NO_DATA} cap`}</span>

        </div>

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// SIGNALS + ARBITER

// ???????????????????????????????????????????????????????????

function SignalsPanel({ signals, arbiter }) {

  const entries = Object.entries(signals || {});

  // Signals excluded from health score ?" stubs or future phases not yet active

  const STUB_SIGNALS = new Set([

    "forge_tailscale_reachable",  // Phase 4 ?" not configured

    "forge_npu_available",         // Phase 3 stub ?" not configured

    "hearth_k3d_healthy",          // known auth issue, low priority

    "forge_model_loaded",          // operational state (idle Forge % fault); forge_lan_reachable covers health

    "forge_gpu_offload",           // operational state (no models loaded != fault)

    "hearth_vega8_models",         // operational state -- model loading, not health

  ]);

  const healthEntries = entries.filter(([k]) => !STUB_SIGNALS.has(k));

  const allLive = healthEntries.every(([, v]) => v);



  return (

    <Panel span="1 / -1">

      <div style={{ display: "flex", gap: 40, alignItems: "flex-start" }}>

        {/* Signals grid */}

        <div style={{ flex: 1 }}>

          <Label>System Signals</Label>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "7px 20px" }}>

            {entries.map(([k, v]) => {

              // v4.7-obs Fix 5: stubs (not-yet-configured / operational-state signals) are
              // KNOWN to be FALSE and were rendering as red errors. Render a neutral gray
              // static dot so they read as "not applicable / pending" rather than "broken".
              const isStub = STUB_SIGNALS.has(k);

              return (

              <div key={k} style={{ display: "flex", alignItems: "center", gap: 6, opacity: isStub ? 0.5 : 1 }}>

                {isStub ? (
                  <span style={{
                    display: "inline-block", width: 5, height: 5, borderRadius: "50%",
                    background: TXM, flexShrink: 0,
                  }} />
                ) : (
                  <PulseDot alive={v} size={5} />
                )}

                <span style={{

                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9),

                  color: isStub ? TXM : (v ? TXD : RSE),

                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",

                }}>

                  {k.replace(/_/g, " ")}

                </span>

              </div>

              );

            })}

          </div>

        </div>



        <div style={{ width: 1, alignSelf: "stretch", background: BORD }} />



        {/* Arbiter status */}

        <div style={{ minWidth: 220 }}>

          <Label>Arbiter</Label>

          <div style={{ display: "flex", gap: 16 }}>

            {[

              ["Broadcaster", arbiter?.broadcaster_connected, "connected"],

              ["Debouncing",  arbiter?.debouncing, "active"],

            ].map(([lbl, val, activeWord]) => (

              <div key={lbl}>

                <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>{lbl}</div>

                <div style={{ display: "flex", alignItems: "center", gap: 5 }}>

                  <PulseDot alive={lbl === "Debouncing" ? !val : val} size={5} />

                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD }}>

                    {lbl === "Debouncing" ? (val ? "active" : "idle") : (val ? "connected" : "disconnected")}

                  </span>

                </div>

              </div>

            ))}

          </div>

          {arbiter?.reason && (

            <div style={{ marginTop: 10 }}>

              <div style={{ fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif", textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 4 }}>Last Transition</div>

              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5), color: TXD }}>{arbiter.reason}</div>

              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM, marginTop: 2 }}>

                {new Date(arbiter.last_transition).toLocaleTimeString()}

              </div>

            </div>

          )}

        </div>



        <div style={{ width: 1, alignSelf: "stretch", background: BORD }} />



        {/* Fleet health summary */}

        <div style={{ minWidth: 120, textAlign: "center" }}>

          <Label style={{ textAlign: "center" }}>Fleet Health</Label>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(28), color: allLive ? GRN : AMB, fontWeight: 600 }}>

            {healthEntries.filter(([, v]) => v).length}

            <span style={{ fontSize: fs(14), color: TXD }}>/{healthEntries.length}</span>

          </div>

          <div style={{ fontSize: fs(9), color: allLive ? GRN : AMB, fontFamily: "Instrument Sans, sans-serif", marginTop: 4 }}>

            {allLive ? "All signals nominal" : "Degraded signals"}

          </div>

        </div>

      </div>

    </Panel>

  );

}



// ???????????????????????????????????????????????????????????

// OBSERVE SURFACE

// ???????????????????????????????????????????????????????????

// Best-available /fleet/ps metadata signal per probe target, so each tile can show the
// METADATA verdict next to the REAL-inference verdict. The gap between them is the point:
// metadata-green + probe-red == "listed but not serving" (the 2026-06-16 incident).
const PROBE_META_SIGNAL = {
  "bifrost:gpu":    (s) => s?.bifrost_ollama_live,
  "hearth:primary": (s) => s?.hearth_ollama_live,
  "hearth:vega8":   (s) => s?.hearth_embed_live,
  "forge:gpu":      (s) => s?.forge_ollama_live,
  // Added with the forge:igor probe target. There is no per-instance metadata signal, so this
  // reuses forge_ollama_live: the two instances share a host, and the host being up is the
  // closest honest signal. It is NOT proof Igor's :11435 instance is serving -- that is exactly
  // the metadata-green/probe-red gap this map exists to expose.
  "forge:igor":     (s) => s?.forge_ollama_live,
  "forge:npu":      (s) => s?.forge_lan_reachable,   // no dedicated NPU metadata signal; LAN reach is closest
};

// ───────────────────────────────────────────────────────────
// LIVE PROBE PANEL  ?" ground-truth runner liveness (real inference)
// ───────────────────────────────────────────────────────────
function LiveProbePanel({ probe, status }) {
  const sig = status?.signals || {};
  const anyDead = PROBE_TARGETS.some(t => { const p = probe?.[t.key]; return p && !p.meta && !p.alive; });
  const anyLie  = PROBE_TARGETS.some(t => { const p = probe?.[t.key]; return p && !p.meta && !p.alive && PROBE_META_SIGNAL[t.key]?.(sig); });

  return (
    <Panel span="1 / -1" style={{ padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXD, letterSpacing: "0.12em" }}>
          LIVE RUNNER PROBE
        </div>
        <div style={{
          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),
          color: anyLie ? RSE : (anyDead ? AMB : GRN),
          background: `${anyLie ? RSE : (anyDead ? AMB : GRN)}12`,
          border: `1px solid ${anyLie ? RSE : (anyDead ? AMB : GRN)}30`,
          borderRadius: 3, padding: "1px 6px",
        }}>{anyLie ? "RUNNER LYING (listed≠serving)" : (anyDead ? "DEGRADED" : "ALL SERVING")}</div>
        <div style={{ marginLeft: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>
          liveness {PROBE_MS / 1000}s (metadata, no inference) - generation {CAPABILITY_PROBE_MS / 60000}m (real /v1) - meta dot = /fleet/ps, probe dot = cheap AND last generation
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: `repeat(${PROBE_TARGETS.length}, 1fr)`, gap: 8 }}>
        {PROBE_TARGETS.map(t => {
          const p = probe?.[t.key];
          const metaUp = !!PROBE_META_SIGNAL[t.key]?.(sig);
          const isMeta = t.kind === "meta" || p?.meta;
          let fg, txt;
          if (isMeta)        { fg = metaUp ? TXD : AMB; txt = `META-ONLY · ${metaUp ? "listed" : "absent"} · NPU too slow to live-probe`; }
          else if (!p)       { fg = TXD; txt = "probing…"; }
          else if (p.alive)  { fg = GRN; txt = p.capAlive ? `SERVING - ${p.ms}ms - gen ${probeAgeLabel(p.capAt)}` : "RESPONDING - gen unproven"; }
          else if (metaUp)   { fg = RSE; txt = `LISTED ≠ SERVING · ${p.detail}`; }
          else               { fg = RSE; txt = `DEAD · ${p.detail}`; }
          const realDead = !isMeta && p && !p.alive;
          return (
            <div key={t.key} style={{
              background: ELEV, border: `1px solid ${realDead ? RSE + "55" : BORDB}`,
              borderRadius: 6, padding: "8px 10px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TX }}>{t.machine}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>{t.slot} :{t.port}</span>
                <span style={{ marginLeft: "auto", fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: TXM }}>{t.kind}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {/* metadata verdict (what Observe USED to trust) */}
                <div style={{ display: "flex", alignItems: "center", gap: 4 }} title="/fleet/ps metadata status">
                  <PulseDot alive={metaUp} size={6} />
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: TXM }}>meta</span>
                </div>
                {/* real-inference verdict (ground truth); n/a for meta-only targets */}
                <div style={{ display: "flex", alignItems: "center", gap: 4 }} title="real /v1 inference probe">
                  {isMeta
                    ? <span style={{ display: "inline-block", width: 6, height: 6, borderRadius: "50%", background: TXM }} />
                    : <PulseDot alive={p ? p.alive : false} size={6} />}
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: TXM }}>{isMeta ? "n/a" : "real"}</span>
                </div>
              </div>
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: fg, marginTop: 6 }}>{txt}</div>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

// ───────────────────────────────────────────────────────────
// FAN-OUT PANEL  ?" demo pane 6: orchestrator -> 4 leaves -> assembly
// ───────────────────────────────────────────────────────────
// SPEC + STUB. A TRUE live fan-out needs a per-dispatch event stream (orchestrator emits
// leaf-start / leaf-token / leaf-done / assembly events) that the backend does NOT expose
// yet. Building it for real is a PROTECTED backend addition -- proposed in paste-pack:
//   v:\AI_Cloud\inhouse\pk\dispatch-journal\PASTEPACK-fanout-stream-backend.md   (TODO)
// Until that stream exists, leaf LIVENESS is real (the fleet probe) and last-event metadata
// is best-effort from /telemetry/recent; streamed output + throughput + assembly are STUBBED
// and labelled as such -- we do not fake a stream we don't have.
const FANOUT_LEAVES = [
  { id: "L1", label: "Leaf 1 · Forge T2",   probeKey: "forge:gpu",      tierMatch: ["2", "2_qwen_coder", "2_devstral"] },
  { id: "L2", label: "Leaf 2 · Bifrost",    probeKey: "bifrost:gpu",    tierMatch: ["1a-coder", "1b", "1a-instruct"] },
  { id: "L3", label: "Leaf 3 · Hearth 1a",  probeKey: "hearth:primary", tierMatch: ["1a-hearth"] },
  { id: "L4", label: "Leaf 4 · Forge NPU",  probeKey: "forge:npu",      tierMatch: ["npu"] },
];

function FanOutPanel({ probe, telemetry, status }) {
  const events = telemetry?.recentEvents || [];
  const sig = status?.signals || {};
  // Best-effort: most recent telemetry event whose tier matches this leaf (real data, not a stream).
  const lastFor = (tierMatch) => {
    for (const ev of events) {
      const tier = ev[6] || ev.tier_used || "";
      if (tierMatch.includes(tier)) return ev;
    }
    return null;
  };

  return (
    <Panel span="1 / -1" style={{ padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXD, letterSpacing: "0.12em" }}>
          LIVE FAN-OUT · DEMO PANE 6
        </div>
        <div style={{
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: AMB,
          background: `${AMB}12`, border: `1px solid ${AMB}30`, borderRadius: 3, padding: "1px 6px",
        }}>SPEC + STUB</div>
      </div>
      <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginBottom: 12 }}>
        Leaf liveness = real probe. Last-event = /telemetry/recent. Streamed output · throughput · assembly
        await a backend orchestrator→leaf event stream (see paste-pack PASTEPACK-fanout-stream-backend.md).
      </div>

      {/* Orchestrator node */}
      <div style={{ display: "flex", justifyContent: "center", marginBottom: 8 }}>
        <div style={{
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: PRP,
          background: `${PRP}15`, border: `1px solid ${PRP}45`, borderRadius: 5, padding: "5px 14px",
        }}>ORCHESTRATOR · fan-out</div>
      </div>

      {/* Leaf tiles */}
      <div style={{ display: "grid", gridTemplateColumns: `repeat(${FANOUT_LEAVES.length}, 1fr)`, gap: 8 }}>
        {FANOUT_LEAVES.map(leaf => {
          const p = probe?.[leaf.probeKey];
          const isMeta = p?.meta;
          const metaUp = !!PROBE_META_SIGNAL[leaf.probeKey]?.(sig);
          const alive = isMeta ? metaUp : (p ? p.alive : null);
          const ev = lastFor(leaf.tierMatch);
          const evLatency = ev ? Math.round(ev[13] || ev.latency_ms || 0) : null;
          const evStatus  = ev ? (ev[15] || ev.status || "?") : null;
          const dotColor  = isMeta ? TXM : (alive === null ? TXD : (alive ? GRN : RSE));
          let liveTxt, liveColor;
          if (isMeta)             { liveColor = AMB; liveTxt = `meta · ${metaUp ? "listed (slow NPU)" : "absent"}`; }
          else if (alive === null){ liveColor = TXD; liveTxt = "probing…"; }
          else if (alive)         { liveColor = GRN; liveTxt = `live · ${p.ms}ms`; }
          else                    { liveColor = RSE; liveTxt = `down · ${p.detail}`; }
          return (
            <div key={leaf.id} style={{
              background: ELEV, border: `1px solid ${(!isMeta && alive === false) ? RSE + "55" : BORDB}`,
              borderRadius: 6, padding: "8px 10px",
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <div style={{ width: 6, height: 6, borderRadius: "50%", background: dotColor, flexShrink: 0 }} />
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TX }}>{leaf.label}</span>
              </div>
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: liveColor }}>
                {liveTxt}
              </div>
              {/* Last real telemetry event for this leaf's tier (best-effort, not a stream) */}
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: TXM, marginTop: 5 }}>
                last: {ev ? <span style={{ color: evStatus === "PASS" ? GRN : RSE }}>{evStatus} · {evLatency}ms</span> : "?"}
              </div>
              {/* STUB rows -- explicitly marked so the demo never implies data it doesn't have */}
              <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: TXM, marginTop: 5, opacity: 0.7 }}>
                stream: <span style={{ color: AMB }}>pending</span> · tok/s: <span style={{ color: AMB }}>?</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Assembly node */}
      <div style={{ display: "flex", justifyContent: "center", marginTop: 8 }}>
        <div style={{
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXM,
          background: ELEV, border: `1px dashed ${BORDB}`, borderRadius: 5, padding: "5px 14px",
        }}>ASSEMBLY · <span style={{ color: AMB }}>stream pending</span></div>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// G5 -- LOCAL TOKEN CLOCK
// ---------------------------------------------------------------------------
// DECISION-G5-take-the-08-07-client-side-design-2026-08-22: a CLIENT-SIDE poll of the
// EXISTING /telemetry/recent. No router change -- main.py is protected and nothing here
// needs it.
//
// THREE THINGS THIS PANEL REFUSES TO CLAIM. Each was measured on 2026-08-25 against the
// live router BEFORE the panel was written:
//
//   1. NOT EXACT. tokens_total is 0 on a small fraction of rows -- 6 of 2,000 measured
//      (0.30%) -- real calls that generated tokens but recorded none. So the headline
//      carries a "~", and the zero-row count is shown rather than quietly absorbed.
//   2. NOT PER-MACHINE. `machine` was "bifrost" and `hardware` was
//      "bifrost-3950x-9070xt-rdna4" on 2,000 of 2,000 rows -- including work that ran on
//      Forge. Those fields are constants, not attribution. Do NOT build a machine split
//      on them; it would look right and be wrong.
//   3. NOT A TOTAL once the window fills. n is a ROW count. A busy session can push its
//      own earliest events out of the window, and when that happens this panel says FLOOR
//      and stops calling the number a total.
//
// local vs cloud is decided by TIER PREFIX, not by cost: all 9 cloud (3-gemini) rows in
// the measured window carried cloud_cost_usd == 0, so cost cannot detect egress.
function isCloudEvent(e) {
  return String(e?.tier_used || "").startsWith("3-") || (e?.cloud_cost_usd || 0) > 0;
}

// sessionStart is a CLIENT-side timestamp in epoch seconds, matching the float timestamps
// the router writes. Reset moves the window forward; nothing is zeroed server-side, so a
// reset can never destroy telemetry.
function useLocalTokenClock() {
  const [sessionStart, setSessionStart] = useState(() => Date.now() / 1000);
  const [snap, setSnap] = useState(null);
  const [link, setLink] = useState("init");

  useEffect(() => {
    let cancelled = false;
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";
    // `force` is for the first sample and for the moment the tab becomes visible. Without
    // it the guard below also swallows the INITIAL fetch, and a panel opened in a background
    // tab sits at "SAMPLING / ~0" indefinitely -- which reads as an idle fleet rather than as
    // a panel that has never run. That is exactly what shipped and was caught on the live
    // portal, where visibilityState is "hidden" even with focus.
    const tick = async (force) => {
      if (!force && hidden()) return;
      let events;
      try {
        const r = await fetch(`${ROUTER}/telemetry/recent?n=${TOKEN_CLOCK_N}`);
        if (!r.ok) throw new Error(r.status);
        events = (await r.json()).events || [];
      } catch {
        // A transport failure is not a zero. Hold the last snapshot and say the link is
        // down, rather than showing a count that appears to have collapsed to 0.
        if (!cancelled) setLink("down");
        return;
      }
      if (cancelled) return;
      const stamps = events.map(e => e?.timestamp || 0);
      const oldest = stamps.length ? Math.min(...stamps) : 0;
      let local = 0, cloud = 0, localCalls = 0, cloudCalls = 0, zeros = 0;
      for (const e of events) {
        if ((e?.timestamp || 0) < sessionStart) continue;
        const tok = e?.tokens_total || 0;
        if (!tok) zeros += 1;
        if (isCloudEvent(e)) { cloud += tok; cloudCalls += 1; }
        else { local += tok; localCalls += 1; }
      }
      setLink("ok");
      setSnap({
        local, cloud, localCalls, cloudCalls, zeros,
        floor: events.length >= TOKEN_CLOCK_N && oldest > sessionStart,
        at: Date.now(),
      });
    };
    tick(true);
    const id = setInterval(() => tick(false), TOKEN_CLOCK_MS);
    const onVis = () => { if (!hidden()) tick(true); };
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVis);
    return () => {
      cancelled = true;
      clearInterval(id);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVis);
    };
  }, [sessionStart]);

  const reset = useCallback(() => {
    setSnap(null);
    setSessionStart(Date.now() / 1000);
  }, []);

  return { ...(snap || {}), sessionStart, link, reset, ready: !!snap };
}

function LocalTokenClock() {
  const c = useLocalTokenClock();
  const local   = c.local || 0;  // rameau-scan: allow -- a property read on the token-clock payload, not a `.local` hostname
  const cloud   = c.cloud || 0;
  const allTok  = local + cloud;
  const pctLoc  = allTok > 0 ? (local / allTok) * 100 : 100;
  const mins    = Math.max((Date.now() / 1000 - c.sessionStart) / 60, 1 / 60);
  const rate    = local / mins;
  const started = new Date(c.sessionStart * 1000).toLocaleTimeString();

  const mark =
    c.link === "down" ? { col: TXD, txt: "LINK DOWN - HOLDING LAST" } :
    c.floor           ? { col: AMB, txt: "WINDOW FULL - FLOOR, NOT A TOTAL" } :
    !c.ready          ? { col: TXD, txt: "SAMPLING" } :
                        { col: GRN, txt: "WINDOW OK" };

  const meta = { fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM };

  return (
    <Panel span="1 / -1" style={{ padding: "14px 16px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXD, letterSpacing: "0.12em" }}>
          LOCAL TOKEN CLOCK
        </div>
        <div style={{
          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),
          color: mark.col, background: `${mark.col}12`, border: `1px solid ${mark.col}30`,
          borderRadius: 3, padding: "1px 6px",
        }}>{mark.txt}</div>
        <div style={{ marginLeft: "auto", ...meta }}>
          since {started} - poll {TOKEN_CLOCK_MS / 1000}s (metadata, no inference) - last {TOKEN_CLOCK_N} rows
        </div>
        <button
          onClick={c.reset}
          style={{
            fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXD,
            background: "transparent", border: `1px solid ${BORD}`, borderRadius: 3,
            padding: "2px 8px", cursor: "pointer",
          }}
        >RESET</button>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "auto 1fr auto", gap: 20, alignItems: "center" }}>
        <div>
          <BigNum value={`~${local.toLocaleString()}`} unit="tokens local" color={GRN} size={34} />
          <div style={{ ...meta, marginTop: 6 }}>
            {(c.localCalls || 0)} local call(s) - {(c.cloudCalls || 0)} cloud - {rate.toFixed(0)} tok/min
          </div>
        </div>

        <div>
          <div style={{ height: 8, background: ELEV, borderRadius: 4, overflow: "hidden", border: `1px solid ${BORD}` }}>
            <div style={{ width: `${pctLoc}%`, height: "100%", background: GRN }} />
          </div>
          <div style={{ ...meta, marginTop: 6 }}>
            {pctLoc.toFixed(1)}% of tokens this session stayed local
            {cloud > 0 ? ` - ~${cloud.toLocaleString()} went to a cloud tier` : " - no cloud tier used"}
          </div>
        </div>

        <div style={{ textAlign: "right" }}>
          {/* Never presented as exact: these are real calls the router recorded with
              tokens_total == 0, so the true count is HIGHER than the headline. */}
          <div style={{ ...meta, color: (c.zeros || 0) > 0 ? AMB : TXM }}>
            {(c.zeros || 0) > 0
              ? `${c.zeros} row(s) reported 0 tokens - true count is HIGHER`
              : "every row in window reported a token count"}
          </div>
          <div style={{ ...meta, marginTop: 4 }}>
            counted by tier, not by machine (the machine field is a constant)
          </div>
        </div>
      </div>
    </Panel>
  );
}

function ObserveSurface({ status, metrics, telemetry, psData, probe, ag }) {

  if (!status || !metrics) {

    return (

      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>

        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12), color: TXD }}>Connecting?</div>

      </div>

    );

  }

  return (

    <div style={{ flex: 1, overflowY: "auto", padding: "18px 20px" }}>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>



        {/* Row 1: Mode hero ?" full width */}

        <ModeHero status={status} />

        {/* Row 1b: Real-inference probe strip ?" full width. Headline of the observe-audit
            brief: ground-truth runner liveness that /fleet/ps metadata cannot give. */}
        <Gated need="Operator" label="Live inference probe" span="1 / -1"><LiveProbePanel probe={probe} status={status} /></Gated>

        {/* Row 1c: G5 LOCAL TOKEN CLOCK -- the demo gate. Self-contained: it
            owns its own 3s poll and holds no state anyone else must thread. */}
        <LocalTokenClock />

        {/* Row 2: Machine cards */}

        <BifrostCard data={status.machines.bifrost} signals={status.signals} psData={psData} tokS={metrics.tokS} probe={probe} tierEp={metrics.tierEndpoint} tierModel={metrics.tierModel} />

        <HearthCard  data={status.machines.hearth}  signals={status.signals} psData={psData} tokS={metrics.tokS} probe={probe} tierEp={metrics.tierEndpoint} tierModel={metrics.tierModel} />

        <ForgeCard   data={status.machines.forge}   signals={status.signals} psData={psData} tokS={metrics.tokS} probe={probe} tierEp={metrics.tierEndpoint} tierModel={metrics.tierModel} />



        {/* Row 3: Band + Tiers + Local split */}

        <BandDonut  bands={metrics.bands} />

        <TierBars   tiers={metrics.tiers} />

        <LocalCloudPanel localPct={metrics.localPct} local={metrics.local} cloud={metrics.cloud} ag={ag} />  {/* rameau-scan: allow -- a property read on the metrics object, not a `.local` hostname */}



        {/* Row 4: Latency + Throughput + Cost */}

        <LatencyPanel   p50={metrics.p50} p90={metrics.p90} p99={metrics.p99} />

        <ThroughputPanel sparkline={metrics.sparkline} />

        <CostPanel

          costDay={telemetry?.cloudSpend?.total_usd ?? metrics.costDay} costWeek={metrics.costWeek}

          costMonth={metrics.costMonth} budgetDay={metrics.budgetDay}

        />



        {/* Row 5: Telemetry feed + Signals */}

        <Gated need="Operator" label="Telemetry feed" span="1 / -1"><TelemetryFeedPanel telemetry={telemetry} /></Gated>

        <Gated need="Admin" label="Signals and arbiter" span="1 / -1"><SignalsPanel signals={status.signals} arbiter={status.arbiter} /></Gated>

        {/* quantum-reward (gate_7 QAOA) panel removed 2026-07-10: in-loop quantum case retired by measurement */}

        {/* Row 6: Live fan-out pane (demo pane 6). SPEC + STUB -- a true orchestrator->leaf
            event stream is a protected backend addition; see paste-pack. */}
        <Gated need="Operator" label="Live fan-out" span="1 / -1"><FanOutPanel probe={probe} telemetry={telemetry} status={status} /></Gated>

      </div>

    </div>

  );

}



function TelemetryFeedPanel({ telemetry }) {

  if (!telemetry) return null;

  const events = telemetry.recentEvents || [];

  const spend = telemetry.cloudSpend || { total_usd: 0, by_tier: {} };

  const bands = telemetry.bands || {};

  const total = Object.values(bands).reduce((a, b) => a + b, 0);



  return (

    <div style={{

      gridColumn: "1 / -1",

      background: SURF, border: `1px solid ${BORD}`,

      borderRadius: 8, padding: "14px 16px",

    }}>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>

        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXD, letterSpacing: "0.12em" }}>

          INFERENCE TELEMETRY

        </div>

        <div style={{

          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),

          color: GRN, background: `${GRN}12`, border: `1px solid ${GRN}30`,

          borderRadius: 3, padding: "1px 6px",

        }}>SQLite A live</div>

        <div style={{ marginLeft: "auto", fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXD }}>

          Cloud spend total: <span style={{ color: spend.total_usd > 0 ? ORG : GRN }}>${spend.total_usd.toFixed(4)}</span>

        </div>

      </div>



      <div style={{ display: "flex", gap: 16 }}>

        {/* Band distribution from SQLite */}

        <div style={{ flex: "0 0 200px" }}>

          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginBottom: 6 }}>BAND DISTRIBUTION · all-time (SQLite)</div>

          {total === 0 ? (

            <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM }}>No events yet</div>

          ) : (

            Object.entries(bands).map(([band, count]) => (

              <div key={band} style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>

                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: BAND_C[band] || TXD, width: 60 }}>{band}</div>

                <div style={{ flex: 1, height: 4, background: ELEV, borderRadius: 2, overflow: "hidden" }}>

                  <div style={{ width: `${(count / total) * 100}%`, height: "100%", background: BAND_C[band] || TXD, borderRadius: 2 }} />

                </div>

                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD, width: 20, textAlign: "right" }}>{count}</div>

              </div>

            ))

          )}

        </div>



        {/* Recent events */}

        <div style={{ flex: 1, overflowX: "auto" }}>

          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginBottom: 6 }}>RECENT EVENTS</div>

          {events.length === 0 ? (

            <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM }}>No events yet ?" send a request through the Router</div>

          ) : (

            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>

              {events.slice(0, 8).map((ev, i) => {

                const tier = ev[6] || ev.tier_used || "?";

                const band = ev[5] || ev.complexity_band || "?";

                const latency = ev[13] || ev.latency_ms || 0;

                const tokens = ev[12] || ev.tokens_total || 0;

                const status = ev[15] || ev.status || "?";

                const ts = ev[1] || ev.timestamp || 0;

                const timeStr = ts ? new Date(ts * 1000).toLocaleTimeString() : "?";

                const bandColor = BAND_C[band] || TXD;

                const tierColor = TIER_C[tier] || TXD;

                return (

                  <div key={i} style={{

                    display: "flex", alignItems: "center", gap: 8,

                    padding: "3px 6px", background: ELEV, borderRadius: 4,

                    fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                  }}>

                    <span style={{ color: TXM, width: 60 }}>{timeStr}</span>

                    <span style={{ color: bandColor, width: 55 }}>{band}</span>

                    <span style={{ color: tierColor, width: 70 }}>{tier}</span>

                    <span style={{ color: TXD, width: 60 }}>{Math.round(latency)}ms</span>

                    <span style={{ color: TXD, width: 45 }}>{tokens}tok</span>

                    <span style={{ color: status === "PASS" ? GRN : RSE }}>{status}</span>

                  </div>

                );

              })}

            </div>

          )}

        </div>



        {/* Cloud spend by tier */}

        {Object.keys(spend.by_tier).length > 0 && (

          <div style={{ flex: "0 0 160px" }}>

            <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginBottom: 6 }}>CLOUD SPEND BY TIER</div>

            {Object.entries(spend.by_tier).filter(([, v]) => v > 0).map(([tier, cost]) => (

              <div key={tier} style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>

                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TIER_C[tier] || TXD }}>{tier}</span>

                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: ORG }}>${cost.toFixed(4)}</span>

              </div>

            ))}

          </div>

        )}

      </div>

    </div>

  );

}



// ???????????????????????????????????????????????????????????

// CONVERSE SURFACE

// ???????????????????????????????????????????????????????????



// demo-edit-4: normalize whatever tool-loop shape the Router returns into a flat list
// of { name, args, result }. Handles the observed variants (_bifrost_tool_loop /
// tools_called / choices[].message.tool_calls) and degrades to [] if none present.
// NOTE for reviewer: the exact field name is unverified against a live tool-loop call
// -- this reads all plausible shapes and renders whatever is there.
function extractToolCalls(data) {
  const out = [];
  if (!data || typeof data !== "object") return out;
  const stringify = (v) => {
    if (v == null) return null;
    if (typeof v === "string") return v;
    try { return JSON.stringify(v); } catch { return String(v); }
  };
  // CC-verified 2026-07-15: _bifrost_tool_loop is an OBJECT {tools_called:[names], reason, ...}
  // -- the names live in .tools_called, not on the object itself.
  const btl = data._bifrost_tool_loop;
  const loop = (btl && Array.isArray(btl.tools_called)) ? btl.tools_called
    : (Array.isArray(btl) ? btl : (data.tools_called || data.tool_calls));
  if (Array.isArray(loop)) {
    for (const t of loop) {
      if (typeof t === "string") { out.push({ name: t, args: null, result: null }); continue; }
      if (!t || typeof t !== "object") continue;
      out.push({
        name: t.name || t.tool || t.function?.name || "tool",
        args: stringify(t.args ?? t.arguments ?? t.input ?? t.function?.arguments),
        result: stringify(t.result ?? t.output ?? t.content ?? t.response),
      });
    }
  }
  const mtc = data.choices?.[0]?.message?.tool_calls;
  if (Array.isArray(mtc)) {
    for (const t of mtc) {
      out.push({
        name: t.function?.name || t.name || "tool",
        args: stringify(t.function?.arguments ?? t.arguments),
        result: null,
      });
    }
  }
  return out;
}

function RoutingPill({ tier, model, latency, tokS, escalation, docsUsed }) {

  const color = TIER_C[tier] || TXD;

  return (

    <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, flexWrap: "wrap" }}>

      <div style={{

        display: "flex", alignItems: "center", gap: 4,

        background: `${color}12`, border: `1px solid ${color}35`,

        borderRadius: 4, padding: "2px 7px",

      }}>

        <div style={{ width: 5, height: 5, borderRadius: 1, background: color }} />

        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color }}>

          {tier}

        </span>

        {model && (

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD }}>

            {model}

          </span>

        )}

        {tokS != null && (

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD }}>

            A {Number(tokS).toFixed(1)} tok/s

          </span>

        )}

        {latency != null && (

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD }}>

            A {latency >= 10000 ? (latency / 1000).toFixed(0) + "s" : latency + "ms"}

          </span>

        )}

      </div>

      {escalation && escalation.length > 1 && (

        <div style={{ display: "flex", alignItems: "center", gap: 3 }}>

          {escalation.map((t, i) => (

            <span key={i} style={{ display: "flex", alignItems: "center", gap: 3 }}>

              {i > 0 && <span style={{ color: TXM, fontSize: fs(8) }}>+'</span>}

              <span style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                color: TIER_C[t] || TXD,

                background: `${TIER_C[t] || TXD}10`,

                border: `1px solid ${TIER_C[t] || TXD}30`,

                borderRadius: 3, padding: "1px 5px",

              }}>{t}</span>

            </span>

          ))}

        </div>

      )}

      {docsUsed > 0 && (

        <div style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: BLU,

          background: `${BLU}10`, border: `1px solid ${BLU}30`,

          borderRadius: 4, padding: "2px 7px",

        }}>

          {docsUsed} doc{docsUsed !== 1 ? "s" : ""}

        </div>

      )}

    </div>

  );

}



// demo-edit-2: side-by-side compare grid. Each cell is fully independent -- its own
// status (loading / done / error) and content, so one failing cell never blanks the
// others. Provenance (local / cloud) is labelled per cell.
function CompareCells({ cells, prompt }) {
  const provLine = (cells || []).map(c => c.prov).join(" / ");
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{
        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD,
        marginBottom: 4, letterSpacing: "0.1em", textTransform: "uppercase",
      }}>
        COMPARE {provLine ? `  -  ${provLine}` : ""}
      </div>
      {prompt && (
        <div style={{
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12), color: TXD,
          marginBottom: 8, fontStyle: "italic",
        }}>{prompt}</div>
      )}
      <div style={{
        display: "grid",
        gridTemplateColumns: `repeat(${Math.min((cells || []).length || 1, 2)}, minmax(0, 1fr))`,
        gap: 8,
      }}>
        {(cells || []).map(cell => {
          const color = TIER_C[cell.tier] || TXD;
          return (
            <div key={cell.key} style={{
              background: SURF, border: `1px solid ${BORD}`,
              borderRadius: 8, padding: "10px 12px",
              display: "flex", flexDirection: "column", gap: 6, minWidth: 0,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{ width: 5, height: 5, borderRadius: 1, background: color, flexShrink: 0 }} />
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color }}>
                  {cell.label}
                </span>
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5),
                  color: cell.prov === "cloud" ? RSE : GRN, marginLeft: "auto",
                  background: `${cell.prov === "cloud" ? RSE : GRN}12`,
                  border: `1px solid ${cell.prov === "cloud" ? RSE : GRN}30`,
                  borderRadius: 3, padding: "1px 5px",
                }}>{cell.prov}</span>
              </div>
              <div style={{
                fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12),
                color: cell.status === "error" ? RSE : TX,
                lineHeight: 1.55, whiteSpace: "pre-wrap", wordBreak: "break-word",
                minHeight: 18,
              }}>
                {cell.status === "loading"
                  ? <span style={{ color: TXD, fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9) }}>...thinking</span>
                  : cell.status === "error"
                    ? <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9) }}>error: {cell.content}</span>
                    : cell.content}
              </div>
              {cell.status === "done" && (
                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: TXM }}>
                  {cell.model || cell.tier}
                  {cell.latency != null ? `  -  ${cell.latency >= 10000 ? (cell.latency / 1000).toFixed(0) + "s" : cell.latency + "ms"}` : ""}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MessageBubble({ msg }) {

  const isUser = msg.role === "user";

  // demo-edit-2: a compare message renders a side-by-side grid instead of a bubble.
  if (msg.compare) return <CompareCells cells={msg.cells} prompt={msg.prompt} />;

  return (

    <div style={{

      display: "flex", flexDirection: "column",

      alignItems: isUser ? "flex-end" : "flex-start",

      marginBottom: 16,

    }}>

      <div style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

        color: isUser ? TXM : TXD,

        marginBottom: 4, letterSpacing: "0.1em", textTransform: "uppercase",

      }}>

        {isUser ? "YOU" : "RAMEAU"}

      </div>

      <div style={{

        maxWidth: "78%",

        background: isUser ? ELEV : SURF,

        border: `1px solid ${isUser ? BORDB : BORD}`,

        borderRadius: isUser ? "12px 12px 2px 12px" : "12px 12px 12px 2px",

        padding: "12px 16px",

      }}>

        <div style={{

          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(13.5),

          color: TX, lineHeight: 1.65, whiteSpace: "pre-wrap", wordBreak: "break-word",

        }}>

          {msg.content}

          {msg.streaming && (

            <span style={{

              display: "inline-block", width: 8, height: 14, marginLeft: 2,

              background: GRN, verticalAlign: "middle",

              animation: "blink 0.8s ease-in-out infinite",

            }} />

          )}

        </div>

      </div>

      {!isUser && msg.tier && (

        <RoutingPill

          tier={msg.tier}

          model={msg.model}

          latency={msg.latency}

          tokS={msg.tokS}

          escalation={msg.escalation}

          docsUsed={msg.docsUsed || 0}

        />

      )}

      {/* demo-edit-4: inline tool-loop calls (Architect persona w/ tool-loop model). */}
      {!isUser && Array.isArray(msg.toolCalls) && msg.toolCalls.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 6, maxWidth: "78%" }}>
          {msg.toolCalls.map((tc, i) => (
            <div key={i} style={{
              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5),
              background: `${PRP}10`, border: `1px solid ${PRP}30`,
              borderRadius: 5, padding: "5px 9px", color: TXD,
            }}>
              <span style={{ color: PRP }}>called </span>
              <span style={{ color: TX }}>{tc.name}</span>
              {tc.args && (
                <span style={{ color: TXD }}>({tc.args.length > 80 ? tc.args.slice(0, 80) + "..." : tc.args})</span>
              )}
              {tc.result != null && (
                <div style={{ marginTop: 3, color: TXD, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  <span style={{ color: PRP }}>{"-> "}</span>
                  {String(tc.result).length > 400 ? String(tc.result).slice(0, 400) + "..." : String(tc.result)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* demo-edit-3: tier-routing explainer for normal (non-compare) responses. */}
      {!isUser && msg.tier && msg.tier !== "error" && (
        <div style={{
          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXD,
          marginTop: 4, opacity: 0.85,
        }}>
          routed to tier {msg.tier}
          {msg.routingReason
            ? ` because ${msg.routingReason}`
            : msg.model
              ? ` (served by ${msg.model})`
              : ""}
        </div>
      )}

      {!isUser && msg.responseMode === "suggest" && (

        <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4 }}>

          <span style={{

            fontSize: fs(11), padding: "2px 8px", borderRadius: 4,

            background: WARN_YEL_BG, color: WARN_YEL,

            border: `1px solid ${WARN_YEL_BORDER}`,

          }}>

            Live data may improve this response

          </span>

        </div>

      )}

    </div>

  );

}



function UploadItem({ item }) {

  const statusColor = { uploading: AMB, indexing: BLU, ready: GRN, error: RSE }[item.status] || TXD;

  return (

    <div style={{

      display: "flex", alignItems: "center", gap: 8,

      padding: "6px 10px", borderRadius: 5,

      background: ELEV, border: `1px solid ${BORD}`, marginBottom: 4,

    }}>

      <div style={{ width: 5, height: 5, borderRadius: 1, background: statusColor, flexShrink: 0 }} />

      <span style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD,

        flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",

      }}>{item.name}</span>

      <span style={{

        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: statusColor,

        textTransform: "uppercase", flexShrink: 0,

      }}>{item.status}</span>

    </div>

  );

}



function ProjectSidebar({

  projects, sessions, activeNs, setActiveNs, demoMode,

  onCreateProject, onDeleteProject,

  onCreateSession, onDeleteSession,

  uploads, onDrop, collapsed, setCollapsed,

}) {

  const [newName, setNewName] = useState("");

  const [creating, setCreating] = useState(false);

  const [dragOver, setDragOver] = useState(false);

  const fileInputRef = useRef(null);
  // C9: creating and uploading are Operator controls; deleting destroys data, so Admin.
  const sideTier = useTier();
  const canOp = atLeast(sideTier, "Operator");
  const canAdmin = atLeast(sideTier, "Admin");



  const handleDrop = (e) => {

    e.preventDefault(); setDragOver(false);

    onDrop(Array.from(e.dataTransfer.files));

  };

  const handleCreate = () => {

    if (!newName.trim()) return;

    onCreateProject(newName.trim()); setNewName(""); setCreating(false);

  };



  if (collapsed) {

    return (

      <div style={{

        width: 32, background: SURF, borderRight: `1px solid ${BORD}`,

        display: "flex", flexDirection: "column", alignItems: "center",

        padding: "12px 0", flexShrink: 0,

      }}>

        <button onClick={() => setCollapsed(false)} style={{

          color: TXD, fontSize: fs(14), padding: 4, borderRadius: 4,

          background: "none", border: "none", cursor: "pointer",

        }}>-</button>

      </div>

    );

  }



  return (

    <div style={{

      width: 220, flexShrink: 0, background: SURF,

      borderRight: `1px solid ${BORD}`,

      display: "flex", flexDirection: "column", overflow: "hidden",

    }}>

      <div style={{

        padding: "12px 14px 10px", borderBottom: `1px solid ${BORD}`,

        display: "flex", justifyContent: "space-between", alignItems: "center",

      }}>

        <span style={{

          fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9),

          color: TXD, textTransform: "uppercase", letterSpacing: "0.12em",

        }}>Knowledge</span>

        <button onClick={() => setCollapsed(true)} style={{

          color: TXM, fontSize: fs(11), background: "none", border: "none", cursor: "pointer",

        }}>-?</button>

      </div>



      <div style={{ flex: 1, overflowY: "auto", padding: "10px 10px 0" }}>

        {/* Projects */}

        <div style={{

          fontSize: fs(8), color: TXM, fontFamily: "Instrument Sans, sans-serif",

          textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 6,

        }}>Projects</div>



        {projects.filter(p => demoMode || !/^demo-/i.test(p.name)).map(p => {

          const nsKey = `proj:${p.name}`;

          const active = activeNs?.key === nsKey;

          return (

            <div key={p.name} style={{

              display: "flex", alignItems: "center", gap: 6,

              padding: "6px 8px", borderRadius: 5, marginBottom: 3, cursor: "pointer",

              background: active ? `${GRN}10` : "transparent",

              border: `1px solid ${active ? GRN + "35" : "transparent"}`,

            }} onClick={() => setActiveNs({ key: nsKey, project: p.name, type: "project" })}>

              <div style={{ width: 6, height: 6, borderRadius: "50%", background: GRN, flexShrink: 0 }} />

              <span style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5),

                color: active ? TX : TXD, flex: 1,

                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",

              }}>{p.name}</span>

              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: TXM }}>

                {p.chunks}

              </span>

              {canAdmin && p.name !== "default" && (

                <button onClick={e => { e.stopPropagation(); onDeleteProject(p.name); }} style={{

                  color: TXM, fontSize: fs(9), background: "none", border: "none",

                  cursor: "pointer", padding: "0 2px", lineHeight: 1,

                }}>A-</button>

              )}

            </div>

          );

        })}



        {creating ? (

          <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>

            <input

              value={newName}

              onChange={e => setNewName(e.target.value)}

              onKeyDown={e => e.key === "Enter" && handleCreate()}

              placeholder="project name"

              autoFocus

              style={{

                flex: 1, background: ELEV, border: `1px solid ${BORDB}`,

                borderRadius: 4, padding: "4px 8px", color: TX,

                fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), outline: "none",

              }}

            />

            <button onClick={handleCreate} style={{

              background: `${GRN}20`, border: `1px solid ${GRN}50`,

              borderRadius: 4, color: GRN, fontSize: fs(10), padding: "2px 6px", cursor: "pointer",

            }}>+</button>

          </div>

        ) : canOp && (

          <button onClick={() => setCreating(true)} style={{

            width: "100%", padding: "5px 8px", borderRadius: 5,

            background: "transparent", border: `1px dashed ${BORD}`,

            color: TXM, fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5),

            cursor: "pointer", marginBottom: 10, textAlign: "left",

          }}>+ New Project</button>

        )}



        {/* Sessions */}

        <div style={{

          fontSize: fs(8), color: TXM, fontFamily: "Instrument Sans, sans-serif",

          textTransform: "uppercase", letterSpacing: "0.12em",

          marginBottom: 6, marginTop: 4, borderTop: `1px solid ${BORD}`, paddingTop: 10,

        }}>Sessions</div>



        {sessions.map(s => {

          const nsKey = `sess:${s.id}`;

          const active = activeNs?.key === nsKey;

          const expiring = s.expires_in_minutes != null && s.expires_in_minutes < 30;

          return (

            <div key={s.id} style={{

              display: "flex", alignItems: "center", gap: 6,

              padding: "6px 8px", borderRadius: 5, marginBottom: 3, cursor: "pointer",

              background: active ? `${AMB}10` : "transparent",

              border: `1px solid ${active ? AMB + "35" : "transparent"}`,

            }} onClick={() => setActiveNs({ key: nsKey, session: s.id, label: s.label, project: s.project || null, type: "session" })}>

              <div style={{ width: 6, height: 6, borderRadius: "50%", background: expiring ? RSE : AMB, flexShrink: 0 }} />

              <span style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5),

                color: active ? TX : TXD, flex: 1,

                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",

              }}>{s.label}</span>

              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7), color: expiring ? RSE : TXM }}>

                {s.expires_in_minutes != null ? `${s.expires_in_minutes}m` : ""}

              </span>

              {canAdmin && (<button onClick={e => { e.stopPropagation(); onDeleteSession(s.id); }} style={{

                color: TXM, fontSize: fs(9), background: "none", border: "none",

                cursor: "pointer", padding: "0 2px", lineHeight: 1,

              }}>A-</button>)}

            </div>

          );

        })}



        {canOp && (<button onClick={onCreateSession} style={{

          width: "100%", padding: "5px 8px", borderRadius: 5,

          background: "transparent", border: `1px dashed ${BORD}`,

          color: TXM, fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5),

          cursor: "pointer", marginBottom: 10, textAlign: "left",

        }}>+ New Session (4h)</button>)}



        {/* Drop zone -- C9: an upload ingests into the KB, so Operator and up */}

        {canOp && <div style={{ borderTop: `1px solid ${BORD}`, paddingTop: 10, marginTop: 4 }}>

          <div style={{

            fontSize: fs(8), color: TXM, fontFamily: "Instrument Sans, sans-serif",

            textTransform: "uppercase", letterSpacing: "0.12em", marginBottom: 8,

          }}>Documents</div>



          {!activeNs && (

            <div style={{

              fontSize: fs(8.5), color: TXM, fontFamily: "Instrument Sans, sans-serif",

              marginBottom: 8, fontStyle: "italic",

            }}>Select a project or session first</div>

          )}



          <div

            onDragOver={e => { e.preventDefault(); if (activeNs) setDragOver(true); }}

            onDragLeave={() => setDragOver(false)}

            onDrop={activeNs ? handleDrop : e => e.preventDefault()}

            onClick={e => { e.stopPropagation(); if (activeNs) fileInputRef.current?.click(); }}

            style={{

              border: `1px dashed ${dragOver ? GRN : activeNs ? BORDB : BORD}`,

              borderRadius: 6, padding: "14px 10px", textAlign: "center",

              background: dragOver ? `${GRN}08` : "transparent",

              cursor: activeNs ? "pointer" : "not-allowed",

              transition: "all 0.15s",

              opacity: activeNs ? 1 : 0.4, marginBottom: 8,

            }}

          >

            <div style={{ fontSize: fs(18), marginBottom: 4 }}>Upload</div>

            <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXD }}>

              Drop files or click

            </div>

            <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(7.5), color: TXM, marginTop: 3 }}>

              PDF A DOCX A MD A TXT

            </div>

          </div>

          <input

            ref={fileInputRef} type="file" multiple accept=".pdf,.docx,.md,.txt"

            style={{ display: "none" }}

            onChange={e => { onDrop(Array.from(e.target.files)); e.target.value = ""; }}

          />

          {uploads.map(u => <UploadItem key={u.id} item={u} />)}

        </div>}

      </div>

    </div>

  );

}



function ConverseSurface({ status, ag }) {

  const [messages, setMessages] = useState([]);

  const [input, setInput] = useState("");

  const [strategy, setStrategy] = useState("INTERACTIVE");

  // demo-edit-1: persona picker. "Default" = normal auto-routed Converse.
  // "Architect" = route through the Router tool-loop model (ARCHITECT_MODEL) and
  // render tool calls inline (demo-edit-4).
  const [persona, setPersona] = useState("Default");

  // demo-edit-2: Compare mode -- one prompt fanned across the tier ladder side-by-side.
  const [compareMode, setCompareMode] = useState(false);

  const [useKb, setUseKb] = useState(false);

  const [demoMode, setDemoMode] = useState(false);
  // C9: COMPARE, RAG and the scope chips are Operator controls. A lower tier must not keep them
  // switched on where the viewer cannot see them, so dropping the tier switches them off.
  const canOp = atLeast(useTier(), "Operator");
  useEffect(() => { if (!canOp) { setCompareMode(false); setUseKb(false); } }, [canOp]);

  const [streaming, setStreaming] = useState(false);

  const [projects, setProjects] = useState([]);

  const [sessions, setSessions] = useState([]);

  const [activeNs, setActiveNs] = useState({ key: null, project: null, type: null });

  const [uploads, setUploads] = useState([]);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const [tierOverride, setTierOverride] = useState(null);

  // C11 (Phase 3): the retriever serves SEVEN inhouse_* collections and this
  // surface queried exactly one. availCols is fetched from GET /collections so
  // an eighth appears without a code change; scopeCols defaults to the single
  // collection used before, so behaviour is unchanged until a chip is clicked.
  const [availCols, setAvailCols] = useState([]);

  const [scopeCols, setScopeCols] = useState(["inhouse_pk"]);

  const threadRef = useRef(null);

  const inputRef = useRef(null);



  useEffect(() => { refreshKb(); }, []);

  // C11: populate the chips from the retriever's own answer, never a hardcoded
  // list. On failure the chips stay hidden and retrieval keeps the default
  // scope -- a dead /collections must not break the RAG path.
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`${INHOUSE}/collections`);
        if (!r.ok) return;
        const d = await r.json();
        if (alive && Array.isArray(d.collections)) setAvailCols(d.collections);
      } catch (e) { /* chips hidden; default scope still works */ }
    })();
    return () => { alive = false; };
  }, []);



  useEffect(() => {

    if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;

  }, [messages]);



  async function refreshKb() {

    try {

      const [p, s] = await Promise.all([

        fetch(`${KB}/projects`).then(r => r.json()),

        fetch(`${KB}/sessions`).then(r => r.json()),

      ]);

      setProjects(Array.isArray(p) ? p : []);

      setSessions(Array.isArray(s) ? s : []);

    } catch (e) { console.warn("bifrost-kb unreachable:", e); }

  }



  async function handleCreateProject(name) {

    await fetch(`${KB}/projects`, {

      method: "POST", headers: { "Content-Type": "application/json" },

      body: JSON.stringify({ name }),

    });

    await refreshKb();

    setActiveNs({ key: `proj:${name}`, project: name, type: "project" });

  }



  async function handleDeleteProject(name) {

    await fetch(`${KB}/projects/${name}`, { method: "DELETE" });

    if (activeNs?.project === name) setActiveNs({ key: null, project: null, type: null });

    await refreshKb();

  }



  async function handleCreateSession() {

    const label = `Session ${new Date().toLocaleTimeString()}`;

    const res = await fetch(`${KB}/sessions`, {

      method: "POST", headers: { "Content-Type": "application/json" },

      body: JSON.stringify({ label, project: activeNs?.project || null }),

    });

    const s = await res.json();

    await refreshKb();

    setActiveNs({ key: `sess:${s.id}`, session: s.id, label: s.label, project: s.project || null, type: "session" });

  }



  async function handleDeleteSession(id) {

    await fetch(`${KB}/sessions/${id}`, { method: "DELETE" });

    if (activeNs?.session === id) setActiveNs({ key: null, project: null, type: null });

    await refreshKb();

  }



  async function handleDrop(files) {

    if (!activeNs) return;

    const valid = files.filter(f => KB_SUPPORTED.some(ext => f.name.endsWith(ext)));

    if (!valid.length) return;

    for (const file of valid) {

      const id = `${file.name}-${Date.now()}`;

      setUploads(u => [...u, { id, name: file.name, status: "uploading" }]);

      const params = activeNs.type === "session"

        ? `?session=${activeNs.session}` : `?project=${activeNs.project}`;

      try {

        setUploads(u => u.map(x => x.id === id ? { ...x, status: "indexing" } : x));

        const fd = new FormData(); fd.append("file", file);

        const res = await fetch(`${KB}/upload${params}`, { method: "POST", body: fd });

        if (!res.ok) throw new Error(await res.text());

        setUploads(u => u.map(x => x.id === id ? { ...x, status: "ready" } : x));

        setTimeout(() => setUploads(u => u.filter(x => x.id !== id)), 4000);

      } catch (e) {

        setUploads(u => u.map(x => x.id === id ? { ...x, status: "error" } : x));

      }

    }

    await refreshKb();

  }



  // demo-edit-2: patch a single compare cell independently.
  function updateCompareCell(msgId, key, patch) {
    setMessages(m => m.map(msg =>
      msg.id === msgId && msg.compare
        ? { ...msg, cells: msg.cells.map(c => c.key === key ? { ...c, ...patch } : c) }
        : msg
    ));
  }

  async function handleSend() {

    const text = input.trim();

    if (!text || streaming) return;

    setInput(""); setStreaming(true);



    const userMsg = { id: Date.now(), role: "user", content: text };

    setMessages(m => [...m, userMsg]);

    const assistantId = Date.now() + 1;



    // --- COMPARE mode (demo-edit-2): fan ONE prompt across the tier ladder ---------------
    // Dispatch BY TIER via X-Tier-Override, one independent fetch per cell. Each cell has
    // its own try/catch + AbortController timeout, so a slow/failed/timed-out cell shows
    // its own error and never breaks the panel or the other cells. Streaming is released
    // immediately -- cells resolve on their own and the input never hangs on the slowest.
    if (compareMode) {
      const cmpHistory = messages
        .filter(mm => !mm.streaming && !mm.compare)
        .map(mm => ({ role: mm.role, content: mm.content }));
      const cells = COMPARE_TIERS.map(c => ({
        ...c, status: "loading", content: "", model: null, latency: null,
      }));
      setMessages(m => [...m, { id: assistantId, role: "assistant", compare: true, cells, prompt: text }]);

      COMPARE_TIERS.forEach(cell => {
        (async () => {
          const ctrl = new AbortController();
          const to = setTimeout(() => ctrl.abort(), COMPARE_TIMEOUT_MS);
          const start = Date.now();
          try {
            const h = { "Content-Type": "application/json", "X-Tier-Override": cell.tier };
            if (activeNs?.project) h["X-Bifrost-Project"] = activeNs.project;
            const resp = await fetch(`${ROUTER}/v1/chat/completions`, {
              method: "POST", headers: h, signal: ctrl.signal,
              body: JSON.stringify({
                model: "bifrost-auto",
                messages: [...cmpHistory, { role: "user", content: text }],
                stream: false,
              }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const d = await resp.json();
            const content = d.choices?.[0]?.message?.content || "(no content)";
            const servedTier = resp.headers.get("x-tier-used") || resp.headers.get("x-bifrost-tier") || d.model || cell.tier;
            updateCompareCell(assistantId, cell.key, {
              status: "done", content, model: d.model || null, tier: servedTier,
              latency: d._bifrost_latency_ms ?? (Date.now() - start),
            });
          } catch (e) {
            updateCompareCell(assistantId, cell.key, {
              status: "error",
              content: e.name === "AbortError" ? "timed out (60s)" : (e.message || String(e)),
            });
          } finally {
            clearTimeout(to);
          }
        })();
      });

      setStreaming(false);
      return;
    }

    // --- ARCHITECT persona (demo-edit-1 + demo-edit-4): route Converse through the -------
    // Router tool-loop model. The Router runs a server-side tool loop for ARCHITECT_MODEL;
    // we render any tool calls inline (demo-edit-4). The very first tool call after a
    // gateway restart can transport-error -- we catch it, show a friendly retryable
    // message, restore the prompt so the user can just hit Send again, and never hang.
    if (persona === "Architect") {
      setMessages(m => [...m, { id: assistantId, role: "assistant", content: "", streaming: true }]);
      const aHistory = messages
        .filter(mm => !mm.streaming && !mm.compare)
        .map(mm => ({ role: mm.role, content: mm.content }));
      try {
        // CEREMONY-92 (2026-08-23): the router tool loop is opt-IN. Without this header
        // the Architect persona gets a plain dispatch and no tool calls, which is the
        // whole reason ARCHITECT_MODEL is a tool-loop alias. Deliberately scoped to
        // Architect -- Compare mode and the other paths stay unenrolled, which keeps the
        // ~1759-token tool prefix off them.
        const aHeaders = { "Content-Type": "application/json", "X-Tool-Loop": "true" };
        if (activeNs?.project) aHeaders["X-Bifrost-Project"] = activeNs.project;
        const start = Date.now();
        const resp = await fetch(`${ROUTER}/v1/chat/completions`, {
          method: "POST", headers: aHeaders,
          body: JSON.stringify({
            model: ARCHITECT_MODEL,
            messages: [...aHistory, { role: "user", content: text }],
            stream: false,
          }),
        });
        if (!resp.ok) throw new Error(`router ${resp.status}`);
        const data = await resp.json();
        const tier = resp.headers.get("x-tier-used") || resp.headers.get("x-bifrost-tier") || data.model || ARCHITECT_MODEL;
        const routingReason = resp.headers.get("x-routing-reason") || resp.headers.get("x-query-features")
          || data._bifrost_routing_reason || null;
        const fullText = data.choices?.[0]?.message?.content || "(no content)";
        const toolCalls = extractToolCalls(data);
        setMessages(m => m.map(msg =>
          msg.id === assistantId
            ? { ...msg, content: fullText, streaming: false, tier, model: data.model || ARCHITECT_MODEL,
                latency: data._bifrost_latency_ms ?? (Date.now() - start),
                tokS: data._bifrost_tok_s ?? null, routingReason, toolCalls, persona: "Architect" }
            : msg
        ));
      } catch (e) {
        // Friendly + retryable: restore the prompt so Send re-fires the same request.
        setInput(text);
        setMessages(m => m.map(msg =>
          msg.id === assistantId
            ? { ...msg, content: `Architect transport error: ${e.message}. This can happen on the first tool call after a gateway restart -- your prompt is restored, press Send to retry.`, streaming: false, tier: "error" }
            : msg
        ));
      }
      setStreaming(false);
      return;
    }

    // --- RAG mode: call /rag endpoint directly ------------------------------------------"?

    if (strategy === "RAG") {

      setMessages(m => [...m, { id: assistantId, role: "assistant", content: "", streaming: true, ragMode: true }]);

      try {

        const project = activeNs?.project || null;

        const res = await fetch(`${ROUTER}/rag`, {

          method: "POST", headers: { "Content-Type": "application/json" },

          body: JSON.stringify({ question: text, project }),

        });

        const data = await res.json();

        setMessages(m => m.map(msg =>

          msg.id === assistantId ? {

            ...msg,

            content: data.answer || data.error || "No answer returned",

            streaming: false,

            ragMode: true,

            ragSources: data.sources || [],

            chunksUsed: data.chunks_used || 0,

            tier: "rag",

          } : msg

        ));

      } catch (e) {

        setMessages(m => m.map(msg =>

          msg.id === assistantId

            ? { ...msg, content: `RAG error: ${e.message}`, streaming: false, tier: "error" }

            : msg

        ));

      }

      setStreaming(false);

      return;

    }




    // --- LOCAL mode: Igor + optional KB via /v1/messages (Anthropic compat) -------
    // Pins to bifrost-igor via v101 anthropic_compat alias mapping.
    // Honors existing useKb toggle for PK-RAG context injection.
    if (strategy === "LOCAL") {
      setMessages(m => [...m, { id: assistantId, role: "assistant", content: "", streaming: true }]);
      let localDocsUsed = 0;
      let localContextPrefix = "";
      if (useKb) try {
        // C1.1: LOCAL+RAG queries inhouse_pk via Bifrost retriever, not bifrost-kb (RFP).
        const inhouseHeaders = { "Content-Type": "application/json" };
        if (activeNs?.project) inhouseHeaders["X-Bifrost-Project"] = activeNs.project;
        const inhouseRes = await fetch(`${INHOUSE}/retrieve`, {
          method: "POST", headers: inhouseHeaders,
          body: JSON.stringify({
            question: text,
            collections: scopeCols.length ? scopeCols : ["inhouse_pk"],
            top_k: 5,
          }),
        });
        if (!inhouseRes.ok) throw new Error(`inhouse retriever ${inhouseRes.status}`);
        const inhouseData = await inhouseRes.json();
        if (inhouseData.chunks?.length) {
          localDocsUsed = inhouseData.chunks.length;
          localContextPrefix = `Use this context from the inhouse knowledge base:\n\n${inhouseData.chunks.map(c => `[${c.source}]\n${c.text}`).join("\n\n---\n\n")}\n\nQuestion: `;
        }
      } catch (e) {
        console.warn("inhouse retriever unavailable:", e);
        /* fall through -- Igor answers without context */
      }
      const localHistory = messages.filter(m => !m.streaming).map(m => ({ role: m.role, content: m.content }));
      const localUserContent = localContextPrefix ? localContextPrefix + text : text;
      try {
        const localStart = Date.now();
        const localHeaders = { "Content-Type": "application/json" };
        if (activeNs?.project) localHeaders["X-Bifrost-Project"] = activeNs.project;
        const resp = await fetch(`${ROUTER}/v1/messages`, {
          method: "POST",
          headers: localHeaders,
          body: JSON.stringify({
            model: "claude-opus-4-7",
            messages: [...localHistory, { role: "user", content: localUserContent }],
            max_tokens: 16384,
          }),
        });
        if (!resp.ok) throw new Error(`router /v1/messages ${resp.status}`);
        const data = await resp.json();
        const fullText = data.content?.[0]?.text || "(no content)";
        const latency = Date.now() - localStart;
        setMessages(m => m.map(msg =>
          msg.id === assistantId
            ? { ...msg, content: fullText, streaming: false, tier: "bifrost-igor",
                latency, docsUsed: localDocsUsed }
            : msg
        ));
      } catch (e) {
        setMessages(m => m.map(msg =>
          msg.id === assistantId
            ? { ...msg, content: `LOCAL error: ${e.message}`, streaming: false, tier: "error" }
            : msg
        ));
      }
      setStreaming(false);
      return;
    }

    // --- INTERACTIVE / AUTOPILOT mode ------------------------------------------------------"?

    let docsUsed = 0;

    let contextPrefix = "";

    if (useKb) try {

      const kbParams = activeNs.type === "session"

        ? { question: text, session: activeNs.session, top_k: 5 }

        : { question: text, project: activeNs.project, top_k: 5 };

      const kbRes = await fetch(`${KB}/retrieve`, {

        method: "POST", headers: { "Content-Type": "application/json" },

        body: JSON.stringify(kbParams),

      });

      const kbData = await kbRes.json();

      if (kbData.chunks?.length) {

        docsUsed = kbData.chunks.length;

        contextPrefix = `Use this context:\n\n${kbData.chunks.map(c => `[${c.source}]\n${c.text}`).join("\n\n---\n\n")}\n\nQuestion: `;

      }

    } catch (e) { /* KB unavailable */ }



    const history = messages.filter(m => !m.streaming).map(m => ({ role: m.role, content: m.content }));

    const userContent = contextPrefix ? contextPrefix + text : text;

    setMessages(m => [...m, { id: assistantId, role: "assistant", content: "", streaming: true }]);



    try {

      const routerHeaders = { "Content-Type": "application/json" };

      if (tierOverride) routerHeaders["X-Tier-Override"] = tierOverride;

      if (activeNs?.project) routerHeaders["X-Bifrost-Project"] = activeNs.project;

      // RAG-hint: a KB question carries injected project context that the classifier would
      // otherwise score COMPLEX -> heavy tier (gemma4/igor, 25-48s). Hint MODERATE so it routes
      // to a fast tier (1a-hearth, ~3s). Skipped if the user pinned a tier chip (respect it).
      if (activeNs?.project && !tierOverride) routerHeaders["X-Complexity-Hint"] = "MODERATE";

      // AUTOPILOT: async submit -> poll -> result.
      // /v1/chat/completions with strategy:AUTOPILOT runs the graph sync and
      // blocks the Router 3-5 min; no browser fetch survives. Use the dedicated
      // /autopilot/* endpoints; stream progress text into the message bubble.
      if (strategy === "AUTOPILOT") {
        try {
          const submitResp = await fetch(`${ROUTER}/autopilot/submit`, {
            method: "POST",
            headers: routerHeaders,
            body: JSON.stringify({
              messages: [...history, { role: "user", content: userContent }],
              strategy: "AUTOPILOT",
            }),
          });
          if (!submitResp.ok) throw new Error(`submit ${submitResp.status}`);
          const submit = await submitResp.json();
          const pollUrl = submit.poll_url;
          const resultUrl = submit.result_url;
          if (!pollUrl || !resultUrl) throw new Error("missing poll_url/result_url");

          const started = Date.now();
          let status = submit.status || "QUEUED";
          // MERGE 7/16 defect-3: PARTIAL (router v220) + FAILED_NEEDS_HUMAN are terminal and
          // carry real assembled_output; without them such runs never exit + burn to TIMEOUT.
          while (!["COMPLETE","PARTIAL","FAILED","FAILED_NEEDS_HUMAN","ERROR","CANCELLED","TIMEOUT"].includes(status)) {
            await new Promise(r => setTimeout(r, 2000));
            let s;
            try {
              s = await (await fetch(`${ROUTER}${pollUrl}`)).json();
            } catch (pe) { continue; }
            status = s.status || status;
            const elapsedSec = Math.round((Date.now() - started) / 1000);
            const progress = `AUTOPILOT ${status} (${s.subtasks_completed || 0}/${s.subtasks_total || 0} subtasks, ${elapsedSec}s)`;
            setMessages(m => m.map(msg =>
              msg.id === assistantId ? { ...msg, content: progress, streaming: true } : msg
            ));
            if (elapsedSec > 600) { status = "TIMEOUT"; break; }
          }

          if (status === "COMPLETE" || status === "PARTIAL") {
            const data = await (await fetch(`${ROUTER}${resultUrl}`)).json();
            const rawText = data.assembled_output || "(no output)";
            // MERGE 7/16 defect-3: PARTIAL carries output but say so plainly rather than
            // passing an incomplete run off as finished. This banner IS the surface fix.
            const fullText = status === "PARTIAL"
              ? `> **PARTIAL RUN** - ${data.subtasks_failed || 0} of ${data.subtasks_total || 0} subtasks FAILED. `
                + `The output below is incomplete and needs review.\n\n${rawText}`
              : rawText;
            setMessages(m => m.map(msg =>
              msg.id === assistantId
                ? { ...msg, content: fullText, streaming: false,
                    tier: "bifrost-autopilot",
                    latency: data.elapsed_seconds ? Math.round(data.elapsed_seconds * 1000) : null,
                    docsUsed,
                    bifrost: { subtasks: data.subtasks_completed, cost_usd: data.cloud_cost_usd, graph_id: submit.graph_id } }
                : msg
            ));
          } else {
            setMessages(m => m.map(msg =>
              msg.id === assistantId
                ? { ...msg, content: `AUTOPILOT ${status}`, streaming: false, tier: "bifrost-autopilot" }
                : msg
            ));
          }
        } catch (e) {
          setMessages(m => m.map(msg =>
            msg.id === assistantId
              ? { ...msg, content: `AUTOPILOT error: ${e.message}`, streaming: false, tier: "bifrost-autopilot" }
              : msg
          ));
        }
        setStreaming(false);
        return;
      }

      const resp = await fetch(`${ROUTER}/v1/chat/completions`, {
        method: "POST",
        headers: routerHeaders,
        body: JSON.stringify({
          model: "bifrost-auto",
          messages: [...history, { role: "user", content: userContent }],
          stream: false,
          "x-strategy": strategy,
        }),
      });

      // v109a-rev: tier still via header (add_routing_headers emits X-Bifrost-Tier);
      // latency / tokS / escalation moved to body envelope (_bifrost_*).
      let tier = resp.headers.get("x-tier-used") || resp.headers.get("x-bifrost-tier");
      const queryFeatures = resp.headers.get("x-query-features") || "";
      const responseMode = resp.headers.get("x-response-mode") || "normal";
      // demo-edit-3 (CC-verified headers 2026-07-15): the live Router emits
      // x-bifrost-routing-source (npu/rule/cascade) + x-bifrost-band + x-bifrost-confidence.
      // Build a human "because ..." from those; x-routing-reason does NOT exist.
      const routingSource = resp.headers.get("x-bifrost-routing-source") || "";
      const routingBand = resp.headers.get("x-bifrost-band") || "";
      const routingConf = resp.headers.get("x-bifrost-confidence") || "";
      const routingReason = resp.headers.get("x-routing-reason") || null;

      if (!resp.ok) throw new Error(`router ${resp.status}`);
      const data = await resp.json();
      if (!tier) tier = data.model || "auto";
      const model = data.model || null;
      const latency = data._bifrost_latency_ms ?? null;
      const tokS = data._bifrost_tok_s ?? null;
      const escalation = data._bifrost_escalation_path || null;
      let reason = routingReason || data._bifrost_routing_reason || data._bifrost_reason || null;
      if (!reason && (routingSource || routingBand)) {
        const src = routingSource ? `the ${routingSource.toUpperCase()} classifier` : "the classifier";
        const band = routingBand ? ` scored this ${routingBand}` : "";
        const conf = routingConf ? ` (conf ${routingConf})` : "";
        reason = `${src}${band}${conf}`.trim();
      }
      if (!reason) reason = queryFeatures || null;
      const fullText = data.choices?.[0]?.message?.content || "(no content)";
      setMessages(m => m.map(msg =>

        msg.id === assistantId

          ? { ...msg, content: fullText, streaming: false, tier, model, latency, tokS, escalation, docsUsed, responseMode, queryFeatures, routingReason: reason }

          : msg

      ));

    } catch (e) {

      setMessages(m => m.map(msg =>

        msg.id === assistantId

          ? { ...msg, content: `Error: ${e.message}`, streaming: false, tier: "error" }

          : msg

      ));

    }

    setStreaming(false);

  }



  const nsLabel = activeNs

    ? activeNs.type === "session" ? `Session: ${activeNs.label}` : `Project: ${activeNs.project}`

    : "No namespace";



  return (

    <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>

      <ProjectSidebar

        projects={projects} sessions={sessions} demoMode={demoMode}

        activeNs={activeNs} setActiveNs={setActiveNs}

        onCreateProject={handleCreateProject} onDeleteProject={handleDeleteProject}

        onCreateSession={handleCreateSession} onDeleteSession={handleDeleteSession}

        uploads={uploads} onDrop={handleDrop}

        collapsed={sidebarCollapsed} setCollapsed={setSidebarCollapsed}

      />



      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Session header */}

        <div style={{

          height: 42, borderBottom: `1px solid ${BORD}`,

          display: "flex", alignItems: "center", padding: "0 16px", gap: 12,

          background: SURF, flexShrink: 0,

        }}>

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5), color: TXD }}>

            {nsLabel}

          </div>

          {activeNs?.type === "session" && (

            <div style={{

              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: AMB,

              background: `${AMB}15`, border: `1px solid ${AMB}35`,

              borderRadius: 3, padding: "1px 6px",

            }}>ephemeral</div>

          )}

          <div style={{ flex: 1 }} />

          {ag && <AirgapBadge ag={ag} />}

          <div style={{

            display: "flex", background: ELEV, borderRadius: 5,

            border: `1px solid ${BORD}`, overflow: "hidden",

          }}>

            {["INTERACTIVE", "LOCAL", "AUTOPILOT"].map(s => (  // RAG_MODE

              <button key={s} onClick={() => setStrategy(s)} style={{

                padding: "4px 10px",

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                background: strategy === s ? (s === "AUTOPILOT" ? `${PRP}20` : s === "LOCAL" ? `${LOCAL_CYAN}20` : `${GRN}15`) : "transparent",

                color: strategy === s ? (s === "AUTOPILOT" ? PRP : s === "LOCAL" ? LOCAL_CYAN : GRN) : TXM,

                border: "none", cursor: "pointer",

                borderRight: s !== "AUTOPILOT" ? `1px solid ${BORD}` : "none",

              }}>{s}</button>

            ))}

          </div>

          {/* demo-edit-1: persona picker. Architect routes Converse through the tool-loop model. */}
          <div style={{
            display: "flex", background: ELEV, borderRadius: 5,
            border: `1px solid ${BORD}`, overflow: "hidden", marginLeft: 4,
          }}>
            {["Default", "Architect"].map(p => (
              <button key={p} onClick={() => setPersona(p)} style={{
                padding: "4px 10px",
                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),
                background: persona === p ? (p === "Architect" ? `${PRP}20` : `${BLU}15`) : "transparent",
                color: persona === p ? (p === "Architect" ? PRP : BLU) : TXM,
                border: "none", cursor: "pointer",
                borderRight: p === "Default" ? `1px solid ${BORD}` : "none",
              }}>{p}</button>
            ))}
          </div>

          {/* demo-edit-2: Compare mode toggle -- fan one prompt across the tier ladder. */}
          {canOp && (<button
            onClick={() => setCompareMode(c => !c)}
            style={{
              fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),
              padding: "4px 10px", cursor: "pointer", marginLeft: 4,
              background: compareMode ? `${GRN}25` : "transparent",
              color: compareMode ? GRN : TXM,
              border: `1px solid ${compareMode ? GRN : BORD}`,
              borderRadius: 4,
            }}
          >COMPARE</button>)}

          {canOp && (<button
            onClick={() => setUseKb(k => !k)}
            style={{
              fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),
              padding: "4px 10px", cursor: "pointer", marginLeft: 4,
              background: useKb ? `${AMB}25` : "transparent",
              color: useKb ? AMB : TXM,
              border: `1px solid ${useKb ? AMB : BORD}`,
              borderRadius: 4,
            }}
          >RAG</button>)}

          <button
            onClick={() => setDemoMode(d => !d)}
            style={{
              fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),
              padding: "4px 10px", cursor: "pointer", marginLeft: 4,
              background: demoMode ? `${ORG}25` : "transparent",
              color: demoMode ? ORG : TXM,
              border: `1px solid ${demoMode ? ORG : BORD}`,
              borderRadius: 4,
            }}
          >DEMO</button>

          <button onClick={() => setMessages([])} style={{

            fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM,

            background: "transparent", border: `1px solid ${BORD}`,

            borderRadius: 4, padding: "3px 8px", cursor: "pointer",

          }}>Clear</button>

        </div>



        {/* Thread */}

        <div ref={threadRef} style={{ flex: 1, overflowY: "auto", padding: "20px 24px" }}>

          {messages.length === 0 ? (

            <div style={{

              height: "100%", display: "flex", flexDirection: "column",

              alignItems: "center", justifyContent: "center", gap: 12, opacity: 0.5,

            }}>

              <div style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(28),

                color: TXM, letterSpacing: "0.1em", fontWeight: 600,

              }}>RAMEAU</div>

              <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12), color: TXM }}>

                {nsLabel} A {strategy}

              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>

                {["Summarize the documents", "What are the key findings?", "Draft a response"].map(s => (

                  <button key={s} onClick={() => { setInput(s); inputRef.current?.focus(); }} style={{

                    fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: TXD,

                    background: ELEV, border: `1px solid ${BORD}`,

                    borderRadius: 6, padding: "6px 12px", cursor: "pointer",

                  }}>{s}</button>

                ))}

              </div>

            </div>

          ) : (

            messages.map(msg => <MessageBubble key={msg.id} msg={msg} />)

          )}

        </div>



        {/* C11 scope chips -- which inhouse_* collections LOCAL+RAG searches.
            Shown only when RAG is on, because they do nothing otherwise. The
            ACTIVE chip uses TX for its label, not the accent: every light-theme
            accent fails WCAG AA 4.5 on SURF (LOCAL_CYAN 3.02), so the accent
            carries in the wash and border while the text stays readable. */}

        {canOp && useKb && availCols.length > 0 && (

          <div style={{ borderTop: `1px solid ${BORD}`, padding: "6px 16px 0", background: SURF, flexShrink: 0, display: "flex", alignItems: "center", gap: 5, flexWrap: "wrap" }}>

            <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginRight: 2, letterSpacing: 0.5 }}>

              SCOPE:

            </span>

            {availCols.map(c => {

              const active = scopeCols.includes(c);

              return (

                <button key={c} title={c} onClick={() => setScopeCols(s => s.includes(c) ? (s.length > 1 ? s.filter(x => x !== c) : s) : [...s, c])} style={{

                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5),

                  padding: "2px 7px", borderRadius: 3, cursor: "pointer",

                  background: active ? `${LOCAL_CYAN}25` : "transparent",

                  color: active ? TX : TXM,

                  border: `1px solid ${active ? LOCAL_CYAN + "60" : BORD}`,

                  transition: "all 0.15s",

                }}>{c.replace(/^inhouse_/, "")}</button>

              );

            })}

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: TXM, marginLeft: 4 }}>

              {scopeCols.length} of {availCols.length}

            </span>

          </div>

        )}

        {/* Tier override pills */}

        <div style={{ borderTop: `1px solid ${BORD}`, padding: "6px 16px 0", background: SURF, flexShrink: 0, display: "flex", alignItems: "center", gap: 5 }}>

          <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: tierOverride ? AMB : TXM, marginRight: 2, letterSpacing: "0.06em" }}>

            TIER:

          </span>

          {[null, "npu", "1a-coder", "1b", "2", "2.5", "2.5-solo"].map(t => {

            const label = t === null ? "AUTO" : t;

            const active = tierOverride === t;

            const color = t === null ? TXM : (TIER_C[t] || TXM);

            return (

              <button key={label} onClick={() => setTierOverride(t)} style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5),

                padding: "2px 7px", borderRadius: 3, cursor: "pointer",

                background: active ? `${color}25` : "transparent",

                color: active ? color : TXM,

                border: `1px solid ${active ? color + "60" : BORD}`,

                transition: "all 0.15s",

              }}>{label}</button>

            );

          })}

          {tierOverride && (

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: AMB, marginLeft: 4 }}>

              s forced +' {tierOverride}

            </span>

          )}

        </div>



        {/* Demo prompt selector */}
        {demoMode && (
          <div style={{
            borderTop: `1px solid ${BORD}`,
            padding: "8px 16px",
            background: SURF,
            display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center",
            flexShrink: 0,
          }}>
            <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: TXM, marginRight: 4 }}>
              DEMO
            </span>
            {DEMO_PROMPTS.map(dp => (
              <button
                key={dp.id}
                onClick={() => {
                  setInput(dp.text);
                  setStrategy(dp.strategy);
                  inputRef.current?.focus();
                }}
                style={{
                  fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),
                  padding: "4px 10px", cursor: "pointer",
                  background: `${dp.color}18`,
                  color: dp.color,
                  border: `1px solid ${dp.color}45`,
                  borderRadius: 4,
                  transition: "all 0.15s",
                }}
              >{dp.label}</button>
            ))}
          </div>
        )}

        {/* Input bar */}

        <div style={{ borderTop: `1px solid ${BORD}`, padding: "12px 16px", background: SURF, flexShrink: 0 }}>

          <div style={{

            display: "flex", gap: 10, alignItems: "flex-end",

            background: ELEV, border: `1px solid ${streaming ? GRN + "50" : BORDB}`,

            borderRadius: 10, padding: "10px 14px", transition: "border-color 0.2s",

          }}>

            <textarea

              ref={inputRef}

              value={input}

              onChange={e => setInput(e.target.value)}

              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}

              placeholder={activeNs ? "Ask anything? Shift+Enter for newline" : "Select a project first"}

              disabled={!activeNs || streaming}

              rows={1}

              style={{

                flex: 1, background: "transparent", border: "none",

                color: TX, fontFamily: "Instrument Sans, sans-serif",

                fontSize: fs(13.5), lineHeight: 1.5, resize: "none", outline: "none",

                maxHeight: 120, overflowY: "auto", minHeight: 22,

              }}

              onInput={e => {

                e.target.style.height = "auto";

                e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";

              }}

            />

            <button

              onClick={handleSend}

              disabled={!input.trim() || !activeNs || streaming}

              style={{

                background: streaming ? "transparent" : `${GRN}25`,

                border: `1px solid ${streaming ? TXM : GRN + "60"}`,

                borderRadius: 7, color: streaming ? TXM : GRN,

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(11),

                padding: "6px 14px", cursor: streaming ? "not-allowed" : "pointer",

                flexShrink: 0, transition: "all 0.15s",

              }}

            >{streaming ? "?" : "Send"}</button>

          </div>

        </div>

      </div>

    </div>

  );

}



// ???????????????????????????????????????????????????????????

// COMMAND SURFACE

// ???????????????????????????????????????????????????????????



// ???????????????????????????????????????????????????????????

// FLEET MANAGER

// ???????????????????????????????????????????????????????????

const MACHINE_C = { bifrost: BLU, hearth: GRN, forge: AMB, cloud: PRP };



export function FleetManager({ fleetConfig, fleetPs }) {

  const [loading, setLoading] = React.useState({});

  const [loadResult, setLoadResult] = React.useState({});

  const [overrides, setOverrides] = React.useState({});



  const tiers = fleetConfig?.tiers || [];

  const endpoints = fleetPs?.endpoints || [];



  function getWarmth(tier) {

    const ep = endpoints.find(e => e.base_url === tier.base_url);

    if (!ep) return { status: "unknown", loaded: [] };

    const loaded = Array.isArray(ep.loaded_models)

      ? ep.loaded_models

      : ep.loaded_models ? [ep.loaded_models] : [];

    return { status: ep.status, loaded };

  }



  async function handleLoad(tier) {

    const model = overrides[tier.tier] || tier.active_model;

    setLoading(p => ({ ...p, [tier.tier]: true }));

    setLoadResult(p => ({ ...p, [tier.tier]: null }));

    try {

      const resp = await fetch(`${ROUTER}/fleet/load`, {

        method: "POST",

        headers: { "Content-Type": "application/json" },

        body: JSON.stringify({ tier: tier.tier, model }),

      });

      const data = await resp.json();

      setLoadResult(p => ({ ...p, [tier.tier]: data.status === "loaded" ? "OK" : "ERR" }));

    } catch (e) {

      setLoadResult(p => ({ ...p, [tier.tier]: "ERR" }));

    }

    setLoading(p => ({ ...p, [tier.tier]: false }));

  }



  const localTiers = tiers.filter(t => t.machine !== "cloud");

  const cloudTiers = tiers.filter(t => t.machine === "cloud");



  function TierRow({ tier }) {

    const warmth = getWarmth(tier);

    const isWarm = warmth.loaded.some(m =>

      m.replace(":latest", "") === tier.active_model.replace(":latest", "")

    );

    const isOnline = warmth.status === "online";

    const mc = MACHINE_C[tier.machine] || TXD;

    const selectedModel = overrides[tier.tier] || tier.active_model;

    const res = loadResult[tier.tier];



    return (

      <div style={{

        display: "grid",

        gridTemplateColumns: "60px 90px 1fr 140px 60px 50px",

        gap: 8, alignItems: "center",

        padding: "7px 10px",

        borderBottom: `1px solid ${BORD}`,

        background: isWarm ? `${GRN}06` : "transparent",

      }}>

        {/* Tier badge */}

        <div style={{

          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10),

          color: TIER_C[tier.tier] || TXD,

          background: `${TIER_C[tier.tier] || TXD}18`,

          border: `1px solid ${TIER_C[tier.tier] || TXD}35`,

          borderRadius: 4, padding: "2px 6px", textAlign: "center",

        }}>{tier.tier}</div>



        {/* Machine */}

        <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: mc }}>

          {tier.machine}

          <span style={{ color: TXM, marginLeft: 4 }}>

            {isOnline ? "-?" : tier.machine === "cloud" ? "?" : "-<"}

          </span>

        </div>



        {/* Model selector */}

        {tier.type === "ollama" ? (

          <select

            value={selectedModel}

            onChange={e => setOverrides(p => ({ ...p, [tier.tier]: e.target.value }))}

            style={{

              background: ELEV, border: `1px solid ${BORDB}`,

              color: TX, fontFamily: "'JetBrains Mono', monospace",

              fontSize: fs(9), borderRadius: 4, padding: "3px 6px",

              cursor: "pointer",

            }}

          >

            {(tier.catalog || [tier.active_model]).map(m => (

              <option key={m} value={m}>{m}</option>

            ))}

          </select>

        ) : (

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD }}>

            {tier.active_model}

          </div>

        )}



        {/* Warmth indicator */}

        <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>

          {warmth.loaded.length > 0

            ? <span style={{ color: isWarm ? GRN : TXM }}>{(warmth.loaded.find(m => m.replace(":latest","") === tier.active_model.replace(":latest","")) || warmth.loaded.find(m => !/embed/i.test(m)) || warmth.loaded[0])?.replace(":latest", "").slice(0,18)}</span>

            : <span style={{ color: TXM }}>idle</span>

          }

        </div>



        {/* Warm/cold dot */}

        <div style={{ textAlign: "center" }}>

          <span style={{

            display: "inline-block", width: 7, height: 7, borderRadius: "50%",

            background: isWarm ? GRN : isOnline ? AMB : (tier.machine === "cloud" ? PRP : TXM),

            boxShadow: isWarm ? `0 0 5px ${GRN}80` : "none",

          }} />

        </div>



        {/* Load button */}

        {tier.type === "ollama" ? (

          <button

            onClick={() => handleLoad(tier)}

            disabled={loading[tier.tier]}

            style={{

              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

              color: loading[tier.tier] ? TXM : res === "OK" ? GRN : res === "ERR" ? RSE : BLU,

              background: "transparent",

              border: `1px solid ${loading[tier.tier] ? TXM : res === "OK" ? GRN + "50" : res === "ERR" ? RSE + "50" : BLU + "40"}`,

              borderRadius: 4, padding: "3px 8px", cursor: loading[tier.tier] ? "not-allowed" : "pointer",

              transition: "all 0.15s",

            }}

          >{loading[tier.tier] ? "?" : res === "OK" ? "o" : res === "ERR" ? "o-" : "load"}</button>

        ) : (

          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textAlign: "center" }}>?"</div>

        )}

      </div>

    );

  }



  return (

    <div style={{ background: SURF, border: `1px solid ${BORD}`, borderRadius: 8, overflow: "hidden" }}>

      {/* Header */}

      <div style={{

        display: "grid",

        gridTemplateColumns: "60px 90px 1fr 140px 60px 50px",

        gap: 8, padding: "6px 10px",

        background: ELEV, borderBottom: `1px solid ${BORDB}`,

      }}>

        {["TIER", "MACHINE", "MODEL", "LOADED", "WARM"].map((h, i) => (

          <div key={i} style={{

            fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

            color: TXM, letterSpacing: "0.08em",

          }}>{h}</div>

        ))}

      </div>



      {/* Local tiers */}

      {localTiers.map(t => <TierRow key={t.tier} tier={t} />)}



      {/* Cloud tiers separator */}

      {cloudTiers.length > 0 && (

        <>

          <div style={{

            padding: "4px 10px", background: `${PRP}10`,

            borderTop: `1px solid ${BORD}`, borderBottom: `1px solid ${BORD}`,

          }}>

            <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8), color: PRP, letterSpacing: "0.08em" }}>

              CLOUD TIERS

            </span>

          </div>

          {cloudTiers.map(t => <TierRow key={t.tier} tier={t} />)}

        </>

      )}



      {/* Footer */}

      <div style={{

        padding: "5px 10px", borderTop: `1px solid ${BORD}`,

        display: "flex", alignItems: "center", gap: 12,

      }}>

        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>

          {localTiers.filter(t => {

            const w = getWarmth(t);

            return w.loaded.length > 0;

          }).length}/{localTiers.length} local warm

        </span>

        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>

          {endpoints.filter(e => e.status === "online").length}/{endpoints.length} endpoints online

        </span>

      </div>

    </div>

  );

}





// -- AutopilotMonitor -- Live DAG status display --

export function AutopilotMonitor({ routerUrl }) {

  const [jobs, setJobs] = React.useState([]);

  const [expanded, setExpanded] = React.useState({});

  // Cleared job ids. CLEAR cannot empty `jobs`: the /autopilot/jobs poll below refills it
  // every 3 s, so an emptied list comes straight back and the button looks broken.
  // Dismissal by graph_id survives the poll, and new work still appears.
  const [dismissed, setDismissed] = React.useState({});



  React.useEffect(() => {

    const poll = async () => {

      try {

        const r = await fetch(`${routerUrl}/autopilot/jobs`);

        if (r.ok) { const d = await r.json(); setJobs(d.jobs || []); }

      } catch {}

    };

    poll();

    const iv = setInterval(poll, 3000);

    return () => clearInterval(iv);

  }, [routerUrl]);



  const statusColor = (s) => {

    if (!s) return TXM;

    if (s === "complete" || s === "PASS" || s === "COMPLETE" || s === "SUCCESS") return GRN;

    if (s === "running" || s === "pending" || s === "RUNNING") return AMB;

    if (s === "failed" || s === "FAIL" || s === "FAILED") return RSE;

    return TXM;

  };






  const visible = jobs.filter(j => !dismissed[j.graph_id]);

  const hiddenCount = jobs.length - visible.length;

  return (

    <div style={{ padding: "8px 14px", display: "flex", flexDirection: "column", gap: 6 }}>

      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>

        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9),

          color: TXD, letterSpacing: "0.12em" }}>

          AUTOPILOT JOBS

        </div>

        <div style={{ marginLeft: "auto", fontFamily: "'JetBrains Mono', monospace",

          fontSize: fs(8), color: TXM }}>

          {visible.length} shown{hiddenCount > 0 ? ` - ${hiddenCount} cleared` : ""}

        </div>

        <button

          onClick={() => setDismissed(d => {

            const next = { ...d };

            jobs.forEach(j => { next[j.graph_id] = true; });

            return next;

          })}

          disabled={!visible.length}

          style={{

            fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8),

            color: visible.length ? TXD : TXM,

            background: "transparent", border: `1px solid ${BORD}`, borderRadius: 3,

            padding: "2px 8px", cursor: visible.length ? "pointer" : "default",

          }}

        >CLEAR</button>

      </div>

      {!visible.length && (

        <div style={{ padding: "12px 0", color: TXD, fontSize: fs(10),

          fontFamily: "Instrument Sans, sans-serif" }}>

          {jobs.length ? "All jobs cleared. New AUTOPILOT work will appear here."

                       : "No active AUTOPILOT jobs. Submit a task above."}

        </div>

      )}

      {visible.slice(0, 8).map(job => {

        const prog = job.subtask_progress || {};

        const keys = Object.keys(prog);

        const done = keys.filter(k => ["PASS","complete","COMPLETE","SUCCESS"].includes(prog[k]?.status)).length;

        const total = job.subtasks_total || keys.length || 1;

        const pct = Math.round((done / total) * 100);

        const isOpen = expanded[job.graph_id];



        return (

          <div key={job.graph_id} style={{

            background: ELEV, border: `1px solid ${BORD}`,

            borderRadius: 6, overflow: "hidden"

          }}>

            <div

              onClick={() => setExpanded(e => ({ ...e, [job.graph_id]: !e[job.graph_id] }))}

              style={{

                padding: "6px 10px", cursor: "pointer", display: "flex",

                alignItems: "center", gap: 8

              }}

            >

              <span style={{

                width: 6, height: 6, borderRadius: "50%",

                background: statusColor(job.status), flexShrink: 0,

                boxShadow: job.status === "RUNNING" ? `0 0 6px ${AMB}` : "none"

              }} />

              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TX, flex: 1 }}>

                {job.graph_id?.slice(0, 12)}

              </span>

              <span style={{ fontSize: fs(8), color: TXD }}>{done}/{total}</span>

              <div style={{ width: 48, height: 3, background: BORD, borderRadius: 2, overflow: "hidden" }}>

                <div style={{

                  width: `${pct}%`, height: "100%",

                  background: pct === 100 ? GRN : AMB,

                  transition: "width 0.3s ease"

                }} />

              </div>

              <span style={{

                fontSize: fs(8), color: statusColor(job.status),

                fontFamily: "'JetBrains Mono', monospace",

                background: `${statusColor(job.status)}15`,

                padding: "1px 5px", borderRadius: 3

              }}>

                {job.status?.toUpperCase() || "?"}

              </span>

            </div>



            {isOpen && keys.length > 0 && (

              <div style={{ borderTop: `1px solid ${BORD}`, padding: "6px 10px" }}>

                {keys.map(k => {

                  const st = prog[k];

                  return (

                    <div key={k} style={{

                      display: "flex", gap: 6, alignItems: "center",

                      padding: "2px 0"

                    }}>

                      <span style={{

                        width: 5, height: 5, borderRadius: "50%",

                        background: statusColor(st?.status), flexShrink: 0

                      }} />

                      <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD, flex: 1 }}>

                        {k.slice(0, 20)}

                      </span>

                      <span style={{ fontSize: fs(8), color: TXM }}>{st?.tier || ""}</span>

                      <span style={{ fontSize: fs(8), color: statusColor(st?.status) }}>{st?.status || "pending"}</span>

                    </div>

                  );

                })}

                {job.elapsed_seconds && (

                  <div style={{ marginTop: 4, fontSize: fs(8), color: TXM }}>

                    Elapsed: {job.elapsed_seconds}s

                  </div>

                )}

              </div>

            )}

          </div>

        );

      })}

    </div>

  );

}

// -- End AutopilotMonitor --





// ???????????????????????????????????????????????????????????

// STUB SURFACES

// ???????????????????????????????????????????????????????????


// ---- MaintenanceSurface ----
// Brief Q Item 6: Maint surface polish.
// 6a: Service health table -- direct poll of each service /health.
// 6b: Scheduled tasks -- hardcoded list (acknowledged tech debt; dispatcher /tasks/status endpoint is v4.8+).
// 6c: Ollama versions per machine -- hardcoded snapshot (manual refresh on ollama upgrades).
// 6d: Mode transitions -- live from ${ARBITER}/transitions.
const MAINT_SERVICES = [
  { name: "Router",            url: "/router/health" },
  { name: "Broadcaster",       url: "/broadcaster/system/status" },
  { name: "Arbiter",           url: "/arbiter/mode" },
  { name: "Bifrost-kb",        url: "/kb/health" },
  // Inhouse Retriever removed 2026-07-09: decommissioned (task Disabled, /retriever->Bifrost:8092 dead,
  // RAG served by /kb :8091). Was a persistent false DOWN. Standalone retriever superseded by the KB service.
  { name: "Dispatcher",        url: "/dispatcher/health" },
  { name: "Prometheus",        url: "/prom/-/ready" },
];

// Read-only observability plane (maint_status_service.py on Bifrost :8095, via nginx /maintapi/).
// Replaces the old hardcoded MAINT_SCHED_TASKS / MAINT_OLLAMA_VERSIONS with live data.
const MAINTAPI = "/maintapi";

// MAINT_SCHED_TASKS and MAINT_OLLAMA_VERSIONS (hardcoded) REMOVED 2026-07-09 -- replaced by live
// data from the read-only maint-status service (/maintapi/tasks/status, /maintapi/ollama-versions).
// The old snapshots were stale (ollama 0.20.2/0.23.2 vs live 0.30.x; 5 fake task rows).

function MaintenanceSurface({ psData }) {
  const [fleetCfg, setFleetCfg] = useState(null);
  const [health, setHealth] = useState(null);
  const [lastPoll, setLastPoll] = useState(null);
  // Brief Q Item 6: extra Maint polish state
  const [serviceHealth, setServiceHealth] = useState({});
  const [transitions, setTransitions] = useState([]);
  // 2026-07-09: live data from the read-only maint-status service (Bifrost :8095 via /maintapi)
  const [liveTasks, setLiveTasks] = useState(null);
  const [liveOllama, setLiveOllama] = useState(null);
  // 2026-07-09: live n8n workflows via arbiter /n8n proxy (Phase 3; key stays server-side)
  const [n8nWf, setN8nWf] = useState(null);
  const [n8nStatus, setN8nStatus] = useState(null);
  // C9: Maint is shown from Operator (read-only health and versions); n8n writes are Admin.
  const canAdmin = atLeast(useTier(), "Admin");

  const refresh = async () => {
    try {
      const [cfg, h, tr] = await Promise.all([
        fetch(`${ROUTER}/fleet/config`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${ROUTER}/health`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${ARBITER}/transitions`).then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      if (cfg) setFleetCfg(cfg);
      if (h) setHealth(h);
      if (tr) setTransitions(Array.isArray(tr) ? tr : []);
      // Live scheduled tasks + ollama versions (read-only observability plane) + n8n workflows (arbiter proxy)
      const [tasksR, ollamaR, n8nR, n8nStatusR] = await Promise.all([
        fetch(`${MAINTAPI}/tasks/status`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${MAINTAPI}/ollama-versions`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${ARBITER}/n8n/workflows`).then(r => r.ok ? r.json() : null).catch(() => null),
        fetch(`${ARBITER}/n8n/status`).then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      if (tasksR) setLiveTasks(tasksR);
      if (ollamaR) setLiveOllama(ollamaR);
      if (n8nR) setN8nWf(n8nR);
      if (n8nStatusR) setN8nStatus(n8nStatusR);
      // Probe each service /health in parallel (Brief Q Item 6a)
      const svcResults = {};
      await Promise.all(MAINT_SERVICES.map(async (svc) => {
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 3000);
          const r = await fetch(svc.url, { signal: ctrl.signal });
          clearTimeout(t);
          svcResults[svc.name] = r.ok ? "up" : "down";
        } catch { svcResults[svc.name] = "down"; }
      }));
      setServiceHealth(svcResults);
      setLastPoll(new Date().toLocaleTimeString());
    } catch(e) { console.warn("Maint poll failed:", e); }
  };

  useEffect(() => { refresh(); const id = setInterval(refresh, 15000); return () => clearInterval(id); }, []);

  // 2026-07-09: gated n8n write actions (activate/deactivate/run) via arbiter. Key stays server-side.
  const [n8nBusy, setN8nBusy] = useState("");
  const n8nAction = async (path, id) => {
    setN8nBusy(id);
    try { await fetch(`${ARBITER}/n8n/${path}`, { method: "POST" }); } catch (e) { console.warn("n8n action failed:", e); }
    setTimeout(() => { refresh(); setN8nBusy(""); }, 900);
  };

  const tiers = fleetCfg?.tiers || [];
  const endpoints = psData?.endpoints || [];

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "18px 20px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(11), color: TXD, letterSpacing: "0.1em" }}>FLEET MAINTENANCE</div>
        <div style={{ flex: 1 }} />
        {health && (
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: GRN, background: `${GRN}15`, border: `1px solid ${GRN}40`, borderRadius: 3, padding: "2px 8px" }}>
            router {health.mode || health.status || "ok"}
          </div>
        )}
        <button onClick={refresh} style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(8.5), color: BLU, background: `${BLU}15`, border: `1px solid ${BLU}40`, borderRadius: 4, padding: "3px 10px", cursor: "pointer" }}>Refresh</button>
        {lastPoll && <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>polled {lastPoll}</span>}
      </div>

      <Panel style={{ marginBottom: 12 }}>
        <Label>Active Tiers</Label>
        {tiers.length === 0 ? (
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: TXM }}>Loading fleet config...</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "80px 1fr 1fr 100px", gap: "6px 16px", alignItems: "center" }}>
            {["tier", "model", "machine", "hardware"].map(h => (
              <div key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textTransform: "uppercase", letterSpacing: "0.1em", paddingBottom: 6, borderBottom: `1px solid ${BORD}` }}>{h}</div>
            ))}
            {tiers.map(t => {
              const tid = t.tier || t.tier_id || "?";
              const tierColor = TIER_C[tid] || TXD;
              return [
                <div key={tid+"a"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: tierColor }}>{tid}</div>,
                <div key={tid+"b"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.active_model || "-"}</div>,
                <div key={tid+"c"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM }}>{t.machine || "-"}</div>,
                <div key={tid+"d"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.hardware || "-"}</div>,
              ];
            })}
          </div>
        )}
      </Panel>

      <Panel style={{ marginBottom: 12 }}>
        <Label>Loaded Models (fleet/ps)</Label>
        {endpoints.length === 0 ? (
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: TXM }}>No endpoints loaded</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {endpoints.map((ep, i) => {
              const models = ep.loaded_models || [];
              const machine = ep.machine || "unknown";
              const color = machine === "bifrost" ? GRN : machine === "hearth" ? BLU : AMB;
              const vramGB = ep.size_vram ? (ep.size_vram / 1e9).toFixed(1) : null;
              return (
                <div key={i} style={{ background: ELEV, borderRadius: 6, padding: "10px 14px", border: `1px solid ${models.length ? BORDB : BORD}`, opacity: ep.status === "online" ? 1 : 0.5 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <div style={{ width: 6, height: 6, borderRadius: 1, background: color, flexShrink: 0 }} />
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color, textTransform: "uppercase" }}>{machine}</span>
                    <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>{ep.base_url}</span>
                    <div style={{ flex: 1 }} />
                    {vramGB && <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: AMB }}>{vramGB}GB VRAM</span>}
                    <PulseDot alive={ep.status === "online"} size={5} />
                  </div>
                  {models.length > 0 && (
                    <div style={{ marginTop: 6, paddingTop: 6, borderTop: `1px solid ${BORD}` }}>
                      {models.map((m, j) => (
                        <div key={j} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9.5), color: TXD }}>
                          {m.replace(/:latest$/, "")}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      {/* Brief Q Item 6a: Service health table */}
      <Panel style={{ marginBottom: 12 }}>
        <Label>Service Health</Label>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 80px 1fr", gap: "6px 16px", alignItems: "center" }}>
          {["service", "status", "endpoint"].map(h => (
            <div key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textTransform: "uppercase", letterSpacing: "0.1em", paddingBottom: 6, borderBottom: `1px solid ${BORD}` }}>{h}</div>
          ))}
          {MAINT_SERVICES.map(svc => {
            const status = serviceHealth[svc.name] || "?";
            const up = status === "up";
            return [
              <div key={svc.name+"a"} style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: TXD }}>{svc.name}</div>,
              <div key={svc.name+"b"} style={{ display: "flex", alignItems: "center", gap: 4, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: up ? GRN : (status === "?" ? TXM : RSE) }}>
                <PulseDot alive={up} size={5} />
                <span>{status.toUpperCase()}</span>
              </div>,
              <div key={svc.name+"c"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{svc.url}</div>,
            ];
          })}
        </div>
      </Panel>

      {/* MaintAgent status (2026-07-09) - read-only, derived from the live tasks feed */}
      <Panel style={{ marginBottom: 12 }}>
        <Label>MaintAgent</Label>
        {(() => {
          const bt = liveTasks?.machines?.Bifrost?.tasks || [];
          const ma = bt.find(t => t.name === "BIFROST-MaintAgent");
          if (!ma) return <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXM }}>{liveTasks ? "BIFROST-MaintAgent not found" : "Loading..."}</div>;
          const ok = ma.lastResult === 0;
          return (
            <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <PulseDot alive={ma.state === "Ready" || ma.state === "Running"} size={6} />
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD }}>{ma.state}</span>
              </div>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM }}>maint_core.py &middot; {ma.trigger || "-"}</span>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXM }}>last {ma.lastRun ? ma.lastRun.replace("T", " ") : "-"}</span>
              <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: ok ? GRN : RSE }}>{ok ? "PASS (0)" : "result " + ma.lastResult}</span>
            </div>
          );
        })()}
      </Panel>

      {/* Scheduled tasks - LIVE both machines: Bifrost local + Hearth via its local collector (phase 2). */}
      <Panel style={{ marginBottom: 12 }}>
        <Label>Scheduled Tasks{liveTasks ? ` (${(liveTasks.machines?.Bifrost?.count || 0) + (liveTasks.machines?.Hearth?.count || 0)} live)` : ""}</Label>
        {!liveTasks ? (
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: TXM }}>Loading live tasks...</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "1.4fr 68px 70px 70px 1.1fr", gap: "5px 14px", alignItems: "center" }}>
            {["task", "machine", "state", "trigger", "last run"].map(h => (
              <div key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textTransform: "uppercase", letterSpacing: "0.1em", paddingBottom: 6, borderBottom: `1px solid ${BORD}` }}>{h}</div>
            ))}
            {[...(liveTasks.machines?.Bifrost?.tasks || []), ...(liveTasks.machines?.Hearth?.tasks || [])].map(t => {
              const sc = (t.state === "Running" || t.state === "Ready") ? GRN : (t.state === "Disabled" ? TXM : AMB);
              const k = (t.machine || "") + "|" + t.name;  // machine-qualified: 'Monitoring' exists on both boxes
              return [
                <div key={k+"a"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.name}</div>,
                <div key={k+"b"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: t.machine === "Hearth" ? BLU : GRN }}>{t.machine}</div>,
                <div key={k+"c"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: sc }}>{t.state}</div>,
                <div key={k+"d"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>{t.trigger}</div>,
                <div key={k+"e"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.lastRun ? t.lastRun.replace("T", " ") : "-"}</div>,
              ];
            })}
          </div>
        )}
        {liveTasks?.machines?.Hearth && liveTasks.machines.Hearth.status !== "live" && (
          <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${BORD}`, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: AMB }}>
            Hearth: {liveTasks.machines.Hearth.note || liveTasks.machines.Hearth.status}
          </div>
        )}
      </Panel>

      {/* Ollama versions - LIVE (2026-07-09) from /maintapi/ollama-versions (probed by IP) */}
      <Panel style={{ marginBottom: 12 }}>
        <Label>Ollama Versions</Label>
        {!liveOllama ? (
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: TXM }}>Loading live versions...</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "90px 70px 62px 1fr 1.4fr", gap: "6px 14px", alignItems: "center" }}>
            {["machine", "version", "status", "endpoint", "notes"].map(h => (
              <div key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textTransform: "uppercase", letterSpacing: "0.1em", paddingBottom: 6, borderBottom: `1px solid ${BORD}` }}>{h}</div>
            ))}
            {(liveOllama.endpoints || []).map((v, i) => {
              const color = v.machine === "Bifrost" ? GRN : v.machine === "Hearth" ? BLU : AMB;
              const up = v.status === "up";
              return [
                <div key={i+"a"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color }}>{v.machine}</div>,
                <div key={i+"b"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: up ? TXD : TXM }}>{v.version || "-"}</div>,
                <div key={i+"c"} style={{ display: "flex", alignItems: "center", gap: 4, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: up ? GRN : RSE }}>
                  <PulseDot alive={up} size={5} /><span>{up ? "up" : "down"}</span>
                </div>,
                <div key={i+"d"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>{v.host}</div>,
                <div key={i+"e"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{v.notes}</div>,
              ];
            })}
          </div>
        )}
      </Panel>

      {/* n8n workflows - LIVE (2026-07-09) via arbiter /n8n proxy. Key stays server-side (Phase 3). */}
      <Panel style={{ marginBottom: 12 }}>
        <Label>n8n Workflows{n8nWf?.data ? ` (${n8nWf.data.filter(w => w.active).length}/${n8nWf.count} active)` : ""}</Label>
        {!n8nWf ? (
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXM }}>
            {n8nStatus && n8nStatus.reachable === false ? `n8n unreachable (${n8nStatus.http_status || n8nStatus.error || "?"})` : "Loading n8n..."}
          </div>
        ) : n8nWf.error ? (
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: RSE }}>{n8nWf.error}</div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            {(n8nWf.data || []).map((w) => {
              const canRun = n8nStatus?.run_configured && (n8nStatus.run_allowlist || []).includes(w.id);
              const busy = n8nBusy === w.id;
              const btn = (bg) => ({ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: bg, background: `${bg}15`, border: `1px solid ${bg}40`, borderRadius: 3, padding: "1px 6px", cursor: busy ? "wait" : "pointer", opacity: busy ? 0.5 : 1 });
              return (
                <div key={w.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <PulseDot alive={!!w.active} size={5} />
                  <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9), color: w.active ? TXD : TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{w.name}</span>
                  <div style={{ flex: 1 }} />
                  {canAdmin && n8nStatus?.write_enabled && (
                    <button disabled={busy} onClick={() => n8nAction(`workflows/${w.id}/${w.active ? "deactivate" : "activate"}`, w.id)} style={btn(w.active ? AMB : GRN)}>{w.active ? "disable" : "enable"}</button>
                  )}
                  {canAdmin && n8nStatus?.write_enabled && canRun && (
                    <button disabled={busy} onClick={() => n8nAction(`run/${w.id}`, w.id)} style={btn(BLU)}>run</button>
                  )}
                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: TXM }}>{w.id}</span>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      {/* Brief Q Item 6d: Mode transitions log (from Arbiter) */}
      <Panel>
        <Label>Mode Transitions (Arbiter)</Label>
        {transitions.length === 0 ? (
          <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: TXM }}>No transitions recorded yet</div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "140px 100px 100px 1.5fr", gap: "6px 16px", alignItems: "center" }}>
            {["timestamp", "from", "to", "trigger"].map(h => (
              <div key={h} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, textTransform: "uppercase", letterSpacing: "0.1em", paddingBottom: 6, borderBottom: `1px solid ${BORD}` }}>{h}</div>
            ))}
            {transitions.slice(0, 20).map((t, i) => {
              const ts = t.timestamp ? new Date(t.timestamp * 1000).toLocaleString() : "?";
              return [
                <div key={i+"a"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>{ts}</div>,
                <div key={i+"b"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TXD }}>{t.from_mode || "?"}</div>,
                <div key={i+"c"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: GRN }}>{t.to_mode || "?"}</div>,
                <div key={i+"d"} style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{t.trigger || "-"}</div>,
              ];
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}



function StubSurface({ name, desc }) {

  return (

    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 10 }}>

      <div style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(18), color: TXD, letterSpacing: "0.05em" }}>{name}</div>

      <div style={{ fontSize: fs(11), color: TXM, fontFamily: "Instrument Sans, sans-serif" }}>{desc}</div>

    </div>

  );

}



// ???????????????????????????????????????????????????????????

// ROOT

// ???????????????????????????????????????????????????????????

export default function BifrostPortal() {

  const [surface, setSurface] = useState("Converse");
  // C9: the tier lives at the root and is provided through TierContext, so every surface can read
  // it -- not only the rail, where it used to be local and so disclosed nothing.
  const [tier, setTier] = useModeTier();
  // A surface hidden by a lower tier cannot stay selected: fall back to Converse, which every tier sees.
  useEffect(() => {
    if (!atLeast(tier, SURFACE_NEED[surface])) setSurface("Converse");
  }, [tier, surface]);

  // One poller for the whole app. Both air-gap badges (Local/Cloud Split card and the session
  // header) read this same state, so they cannot disagree with each other.
  const ag = useAirgap();

  const [status,  setStatus]  = useState(null);

  const [metrics, setMetrics] = useState(null);

  const [tick,    setTick]    = useState(0); // force re-render timestamp

  const [psData,  setPsData]  = useState(null);

  const [fleetConfig, setFleetConfig] = useState(null);

  const [fleetPs,     setFleetPs]     = useState(null);

  const [telemetry, setTelemetry] = useState({ bands: {}, cloudSpend: { total_usd: 0, by_tier: {} }, recentEvents: [] });

  // Real-inference fleet probe (see runFleetProbe). Independent of the 15s metadata poll.
  const [probe, setProbe] = useState({});



  const poll = useCallback(async () => {

    const [s, m, a, ps, tel, fc, fps] = await Promise.all([fetchStatus(), fetchMetrics(), fetchArbiter(), fetchPs(), fetchTelemetry(), fetchFleetConfig(), fetchFleetPs()]);

    // Snap-back defense: Arbiter force-lock wins over Broadcaster hardware detection

    const OFFLINE_GUARD = new Set(["WORKSHOP_OFFLINE", "JARVIS_OFFLINE"]);

    const effectiveMode = OFFLINE_GUARD.has(a?.confirmed_mode) ? a.confirmed_mode : s.mode;

    setStatus({ ...s, mode: effectiveMode, arbiter: a });

    // B1 (2026-05-29): preserve the latency trio ATOMICALLY. The old per-key
    // fallback could stitch a stale p90 onto a fresh p99, producing an impossible
    // ordering (p90 > p99). Only fall back to the previous trio when all three
    // percentiles are missing/NaN (the genuine idle case). Plus a monotonicity
    // guard against a partial sample that slips through.
    setMetrics(prev => {
      if (!prev || !m) return m;
      const merged = { ...m };
      const _bad = (v) => v == null || Number.isNaN(v);
      if (_bad(merged.p50) && _bad(merged.p90) && _bad(merged.p99)) {
        merged.p50 = prev.p50;
        merged.p90 = prev.p90;
        merged.p99 = prev.p99;
      }
      if (!_bad(merged.p90) && !_bad(merged.p99) && merged.p90 > merged.p99) {
        merged.p90 = merged.p99;
      }
      return merged;
    });

    setPsData(ps);

    if (fc) setFleetConfig(fc);

    if (fps) setFleetPs(fps);

    setTelemetry(tel);

    setTick(Date.now());

  }, []);



  useEffect(() => {

    poll();

    const id = setInterval(poll, POLL_MS);

    return () => clearInterval(id);

  }, [poll]);

  // PACKET-80 Option C: TWO cadences instead of one.
  //   liveTick -- every PROBE_MS, cheap, metadata only, never generates.
  //   capTick  -- every CAPABILITY_PROBE_MS, the original real-inference sweep.
  // Both are skipped while the tab is hidden, so a backgrounded Observe tab issues nothing.
  // A dead-runner node still goes RED even while /fleet/ps reports it "online" -- that now
  // comes from the capability verdict outranking the cheap one in mergeProbe().
  useEffect(() => {
    let cancelled = false;
    let live = null;
    let cap  = null;
    let liveFails = 0;
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";
    const publish = () => { if (!cancelled) setProbe(mergeProbe(live, cap)); };

    const liveTick = async () => {
      if (hidden()) return;
      const l = await runLivenessSweep();
      if (cancelled) return;
      if (l) {
        liveFails = 0;
        live = l;
      } else {
        // Transport failure, not a fleet failure. Hold the previous verdict until it is
        // sustained, so a single proxy 502 does not paint the whole fleet red.
        liveFails += 1;
        if (liveFails < LIVENESS_FAIL_TOLERANCE) return;
        live = livenessAllUnreachable();
      }
      publish();
    };

    const capTick = async () => {
      if (hidden()) return;
      const r = await runFleetProbe();
      if (cancelled) return;
      const at = Date.now();
      const next = {};
      for (const k of Object.keys(r)) next[k] = { ...r[k], at };
      cap = next;
      publish();
    };

    liveTick();
    capTick();                                   // prove generation once at load, then hourly
    const idLive = setInterval(liveTick, PROBE_MS);
    const idCap  = setInterval(capTick,  CAPABILITY_PROBE_MS);
    return () => { cancelled = true; clearInterval(idLive); clearInterval(idCap); };
  }, []);



  return (

    <TierContext.Provider value={tier}>

      <style>{`

        /* 2.3 (D4): the Google Fonts @import lived here and was the Portal's LAST outbound
           asset request. The three families are now self-hosted from src/assets/fonts via
           @font-face in index.css. rev4 says to delete this from index.css as well -- it was
           never there; this was the only site in the tree. */

        html, body { background: ${BG}; margin: 0; padding: 0; }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        ::-webkit-scrollbar { width: 5px; height: 5px; }

        ::-webkit-scrollbar-track { background: ${BG}; }

        ::-webkit-scrollbar-thumb { background: ${BORD}; border-radius: 3px; }

        ::-webkit-scrollbar-thumb:hover { background: ${BORDB}; }

        button { background: none; border: none; cursor: pointer; }

        @keyframes blink {

          0%, 100% { opacity: 1; }

          50%       { opacity: 0.35; }

        }

      `}</style>



      <div style={{

        height: "100vh", background: BG, color: TX,

        display: "flex", flexDirection: "column",

        fontFamily: "Instrument Sans, sans-serif",

        overflowX: "hidden",

      }}>

        <TopNav status={status} setStatus={setStatus} psData={psData} tierModel={metrics?.tierModel} />

        {/* 2.2-b: the shell was a single column with the tab strip inside TopNav. The rail is a
            sibling of the surfaces, not of the header, so this row holds the rail and a content
            column. The status bar stays OUTSIDE the row, full width, where it was. */}
        <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden" }}>

          <SurfaceRail surface={surface} setSurface={setSurface} tier={tier} setTier={setTier} />

          <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0, minHeight: 0, overflow: "hidden" }}>



            <div style={{ display: surface === "Observe"  ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}><ObserveSurface status={status} metrics={metrics} telemetry={telemetry} psData={psData} probe={probe} ag={ag} /></div>

            <div style={{ display: surface === "Converse" ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}><ConverseSurface status={status} ag={ag} /></div>

            <div style={{ display: surface === "Command"  ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}><Gated need={SURFACE_NEED.Command}><CommandSurface status={status} psData={psData} fleetConfig={fleetConfig} fleetPs={fleetPs} /></Gated></div>
            <div style={{ display: surface === "Fanout" ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}><Gated need={SURFACE_NEED.Fanout}><ArchitectFanoutSurface status={status} /></Gated></div>
            <div style={{ display: surface === "Maint" ? "flex" : "none", flex: 1, flexDirection: "column", minHeight: 0, overflow: "hidden" }}><Gated need={SURFACE_NEED.Maint}><MaintenanceSurface psData={psData} /></Gated></div>

          </div>{/* /content column */}
        </div>{/* /rail+content row */}



        {/* Status bar */}

        <div style={{

          height: 24, borderTop: `1px solid ${BORD}`,

          display: "flex", alignItems: "center",

          padding: "0 16px", gap: 16, background: SURF, flexShrink: 0,

        }}>

          {/* NOT `!!status`: since 2.6 a failed fetch returns a SHAPED object rather than null,
              so status is truthy while disconnected and this dot pulsed alive against a dead
              backend. The shape was right for the ~30 consumers that index into status; it just
              inverted this one boolean. */}
          <PulseDot alive={!!status && !status.unreachable} size={5} />

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM }}>

            {/* The timestamp is when the Portal last TRIED, not when it last succeeded, so the
                label has to say which. Before this it read "Last poll <time>" while the badge
                above it read NO DATA. Same family as 2.6 (D5), outside 2.6's executed scope. */}
            {!tick
              ? "Polling?"
              : status?.unreachable
                ? `No data — last tried ${new Date(tick).toLocaleTimeString()}`
                : `Last poll ${new Date(tick).toLocaleTimeString()}`}

          </span>

          <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXM, marginLeft: "auto" }}>

            RAMEAU Portal v0.1 A {BROADCASTER}

          </span>

        </div>

      </div>

    </TierContext.Provider>

  );

}


































