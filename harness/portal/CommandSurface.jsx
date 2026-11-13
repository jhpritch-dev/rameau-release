import React, { useState } from "react";

// Shared primitives + sibling surfaces still live in BifrostPortal.jsx.
// They are hoisted `function` declarations, so this back-import forms a safe
// ES-module cycle (every reference below is evaluated at render time, long
// after both modules finish loading).
import { Panel, Label, PulseDot, FleetManager, AutopilotMonitor } from "./BifrostPortal";
// ModeTier imports only ./theme/tokens, so this adds no edge to the BifrostPortal cycle.
import { useTier, atLeast, Gated } from "./ModeTier";
import { SURF, ELEV, BORD, BORDB, TX, TXD, TXM, GRN, BLU, AMB, ORG, RSE, PRP,
  fs,
} from "./theme/tokens";

// ???????????????????????????????????????????????????????????
// CONFIG / DESIGN TOKENS
// Re-declared from BifrostPortal.jsx (Phase 1: the palette now comes from ./theme/tokens).
// Keep in sync with the source-of-truth definitions at the top of BifrostPortal.jsx.
// ???????????????????????????????????????????????????????????
const ROUTER  = "/router";
const ARBITER = "/arbiter";

// Palette moved to ./theme/tokens.js (Phase 1); imported above.

// Consolidated mode-switch helper (was duplicated across the /privacy on and
// /privacy off branches): POST the target mode to Arbiter (bypasses debounce)
// then to Router (keeps cascade tables correct immediately).
async function setSystemMode(mode) {
  await fetch(`${ARBITER}/mode`, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }) });
  await fetch(`${ROUTER}/mode`, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }) });
}

// v162 T3-ESCALATION-REDESIGN: collapsible Cloud Tiers (T3) panel.
// Reads the fleetConfig prop (same /fleet/config payload FleetManager uses),
// filters tiers starting with "3-", and per tier shows model, _profile_affinity
// badges, an escalation/active indicator, and a Test probe (X-Tier-Override ->
// tiny chat call, reports HTTP status + latency). Mode badge: "Capability-routed"
// when any T3 tier is active (escalation_only=false), else "Fallback-only".
const TAG_COLORS = {
  agent: BLU, coding: GRN, speed: AMB, trivial: TXD, chat: BLU, fast: AMB,
  research: PRP, fallback: TXD, cost: AMB, reasoning: PRP, complex: ORG,
  vision: BLU, quality: GRN, judgment: PRP, max: RSE, web: BLU, current: GRN,
  rag: PRP, document: BLU,
};

function CloudTiersPanel({ fleetConfig }) {
  const [expanded, setExpanded] = useState(false);
  const [tests, setTests] = useState({});

  const t3 = (fleetConfig?.tiers || []).filter(t => String(t.tier || "").startsWith("3-"));
  const anyActive = t3.some(t => t.escalation_only !== true);
  const modeLabel = anyActive ? "Capability-routed" : "Fallback-only";
  const modeColor = anyActive ? GRN : AMB;

  async function probe(tierId) {
    setTests(prev => ({ ...prev, [tierId]: { running: true } }));
    const t0 = performance.now();
    try {
      const resp = await fetch(`${ROUTER}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Tier-Override": tierId },
        body: JSON.stringify({
          model: "bifrost-auto",
          messages: [{ role: "user", content: "ping" }],
          stream: false, max_tokens: 8,
        }),
      });
      const ms = Math.round(performance.now() - t0);
      setTests(prev => ({ ...prev, [tierId]: { running: false, status: resp.status, ms } }));
    } catch (e) {
      const ms = Math.round(performance.now() - t0);
      setTests(prev => ({ ...prev, [tierId]: { running: false, status: "ERR", ms, err: e.message } }));
    }
  }

  return (
    <Panel span="1 / -1" style={{ borderColor: `${BLU}30` }}>
      <div onClick={() => setExpanded(e => !e)}
        style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
        <Label style={{ margin: 0 }}>Cloud Tiers (T3)</Label>
        <span style={{
          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: modeColor,
          background: `${modeColor}15`, border: `1px solid ${modeColor}40`,
          borderRadius: 4, padding: "2px 8px",
        }}>{modeLabel}</span>
        <span style={{
          marginLeft: "auto", fontFamily: "'JetBrains Mono', monospace",
          fontSize: fs(9), color: TXD,
        }}>{t3.length} tiers {expanded ? "[-]" : "[+]"}</span>
      </div>
      {expanded && (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
          {t3.map(t => {
            const esc = t.escalation_only === true;
            const tags = t._profile_affinity || [];
            const tr = tests[t.tier] || {};
            const sc = tr.status === 200 ? GRN : RSE;
            return (
              <div key={t.tier} style={{
                display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
                background: ELEV, border: `1px solid ${BORD}`, borderRadius: 6, padding: "7px 10px",
              }}>
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TX, minWidth: 96,
                }}>{t.tier}</span>
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5), color: TXD,
                }}>{t.active_model || "-"}</span>
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: esc ? AMB : GRN,
                  background: `${esc ? AMB : GRN}15`, border: `1px solid ${esc ? AMB : GRN}40`,
                  borderRadius: 3, padding: "1px 6px",
                }}>{esc ? "escalation" : "active"}</span>
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  {tags.map(tag => {
                    const c = TAG_COLORS[tag] || TXD;
                    return (
                      <span key={tag} style={{
                        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(7.5), color: c,
                        background: `${c}12`, border: `1px solid ${c}33`,
                        borderRadius: 3, padding: "1px 5px",
                      }}>{tag}</span>
                    );
                  })}
                </div>
                <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
                  {tr.status != null && (
                    <span style={{
                      fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: sc,
                    }}>{tr.status}{tr.ms != null ? ` ${tr.ms}ms` : ""}</span>
                  )}
                  <button onClick={() => probe(t.tier)} disabled={tr.running} style={{
                    fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),
                    color: tr.running ? TXM : BLU,
                    background: tr.running ? "transparent" : `${BLU}18`,
                    border: `1px solid ${tr.running ? TXM : BLU + "50"}`,
                    borderRadius: 4, padding: "3px 10px",
                    cursor: tr.running ? "not-allowed" : "pointer",
                  }}>{tr.running ? "..." : "Test"}</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

function CommandSurface({ status, psData, fleetConfig, fleetPs }) {
  // C9: Command is shown from Operator. The fleet mode switch (/privacy), model loads and the
  // cloud-tier config view are Admin.
  const isAdmin = atLeast(useTier(), "Admin");

  const [apInput, setApInput]     = useState("");

  const [apRunning, setApRunning] = useState(false);

  const [apResult, setApResult]   = useState(null);

  const [slashInput, setSlashInput] = useState("");

  const [slashResult, setSlashResult] = useState(null);

  const [slashRunning, setSlashRunning] = useState(false);



  // --- AUTOPILOT submit ---------------------------------------------------------

  async function handleAutopilot() {

    const text = apInput.trim();

    if (!text || apRunning) return;

    setApRunning(true);

    setApResult(null);

    try {

      const submitResp = await fetch(`${ROUTER}/autopilot/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bifrost-Project": "bifrost-self" },
        body: JSON.stringify({
          messages: [{ role: "user", content: text }],
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
      let latestStatus = submit;
      while (!["COMPLETE","FAILED","ERROR","CANCELLED","TIMEOUT"].includes(status)) {
        await new Promise(r => setTimeout(r, 2000));
        try {
          latestStatus = await (await fetch(`${ROUTER}${pollUrl}`)).json();
        } catch (pe) { continue; }
        status = latestStatus.status || status;
        const elapsedSec = Math.round((Date.now() - started) / 1000);
        setApResult({
          graph_id: submit.graph_id, status,
          elapsed_seconds: elapsedSec,
          subtasks_completed: latestStatus.subtasks_completed || 0,
          subtasks_total: latestStatus.subtasks_total || 0,
          prompt: text, progress: true,
        });
        if (elapsedSec > 600) { status = "TIMEOUT"; break; }
      }

      if (status === "COMPLETE") {
        const data = await (await fetch(`${ROUTER}${resultUrl}`)).json();
        setApResult({ ...data, graph_id: submit.graph_id });
      } else {
        setApResult({ ...latestStatus, graph_id: submit.graph_id, status });
      }

    } catch (e) {

      setApResult({ error: e.message });

    }

    setApRunning(false);

  }



  // --- Slash command submit ---------------------------------------------------

  async function handleSlashCmd(cmd) {

    setSlashInput(cmd);

    const trimmed = cmd.trim();

    if (!trimmed || slashRunning) return;
    if (trimmed.startsWith("/privacy") && !isAdmin) {
      setSlashResult("/privacy switches the fleet mode, which is shown at the Admin detail level.");
      return;
    }

    setSlashRunning(true);

    setSlashResult(null);



    if (trimmed === "/privacy on") {

      try {

        await setSystemMode("JARVIS_OFFLINE");

        setSlashResult(`Privacy ON ?" mode: JARVIS_OFFLINE`);

      } catch (e) {

        setSlashResult(`Error: ${e.message}`);

      }

      setSlashRunning(false);

      return;

    }

    if (trimmed === "/privacy off") {

      try {

        await setSystemMode("JARVIS");

        setSlashResult(`Privacy OFF ?" mode: JARVIS`);

      } catch (e) {

        setSlashResult(`Error: ${e.message}`);

      }

      setSlashRunning(false);

      return;

    }



    // All other commands go through Router chat

    try {

      const resp = await fetch(`${ROUTER}/v1/chat/completions`, {

        method: "POST",

        headers: { "Content-Type": "application/json", "X-Bifrost-Project": "bifrost-self" },

        body: JSON.stringify({

          model: "bifrost-auto",

          messages: [{ role: "user", content: trimmed }],

          stream: false,

        }),

      });

      const data = await resp.json();

      const text = data.choices?.[0]?.message?.content || JSON.stringify(data);

      setSlashResult(text);

    } catch (e) {

      setSlashResult(`Error: ${e.message}`);

    }

    setSlashRunning(false);

  }



  const bifrost = psData?.bifrost;

  const hearth  = psData?.hearth;

  const vega8   = psData?.hearth_vega8;

  const forge   = psData?.forge;



  const PROFILE_C_B = { "B-Light": GRN, "B-Dual": BLU, "B-Heavy": ORG };

  const PROFILE_C_F = { "F-Multi": ORG, "F-Max": RSE };



  const SLASH_CMDS = [

    ["/status",          "System status"],

    ["/cost today",      "Cloud spend today"],

    ["/bifrost light",   "Switch Bifrost +' B-Light"],

    ["/bifrost dual",    "Switch Bifrost +' B-Dual"],

    ["/bifrost heavy",   "Switch Bifrost +' B-Heavy"],

    ["/forge multi",     "Switch Forge +' F-Multi"],

    ["/forge max",       "Switch Forge +' F-Max"],

    ["/privacy on",      "Enable WORKSHOP-OFFLINE"],

    ["/privacy off",     "Disable privacy mode"],

    ["/autopilot status","AUTOPILOT queue status"],

  ];



  const apBifrost = psData ? {

    bif: bifrost?.bifrost || bifrost,

  } : null;



  return (

    <div style={{ flex: 1, overflowY: "auto", padding: "18px 20px" }}>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, maxWidth: 1100 }}>



        {/* --- Fleet Manager --- C9: a model load swaps VRAM, so Admin */}

        <Gated need="Admin"><Panel span="1 / -1" style={{ borderColor: `${AMB}30`, padding: 0, overflow: "hidden" }}>

          <div style={{ padding: "10px 14px", borderBottom: `1px solid ${BORD}`, display: "flex", alignItems: "center", gap: 8 }}>

            <Label style={{ margin: 0 }}>Fleet Manager</Label>

            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM, marginLeft: "auto" }}>

              {fleetPs ? `polled ${new Date().toLocaleTimeString()}` : "polling?"}

            </span>

          </div>

          <div style={{ padding: "10px 14px" }}>

            <FleetManager fleetConfig={fleetConfig} fleetPs={fleetPs} />

          </div>

        </Panel></Gated>



        {/* --- Cloud Tiers (T3) --- */}

        <Gated need="Admin"><CloudTiersPanel fleetConfig={fleetConfig} /></Gated>



        {/* --- AUTOPILOT Launcher --- */}

        <Panel span="1 / -1" style={{ borderColor: `${PRP}35`, background: `linear-gradient(140deg, ${SURF} 0%, ${PRP}08 100%)` }}>

          <Label>AUTOPILOT Launcher</Label>

          <div style={{

            display: "flex", gap: 8, marginBottom: 12,

            background: ELEV, border: `1px solid ${apRunning ? PRP + "60" : BORDB}`,

            borderRadius: 8, padding: "10px 14px", transition: "border-color 0.2s",

          }}>

            <textarea

              value={apInput}

              onChange={e => setApInput(e.target.value)}

              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleAutopilot(); } }}

              placeholder="Describe a task to decompose and execute autonomously?"

              disabled={apRunning}

              rows={2}

              style={{

                flex: 1, background: "transparent", border: "none",

                color: TX, fontFamily: "Instrument Sans, sans-serif",

                fontSize: fs(13), lineHeight: 1.5, resize: "none", outline: "none",

              }}

            />

            <button

              onClick={handleAutopilot}

              disabled={!apInput.trim() || apRunning}

              style={{

                background: apRunning ? "transparent" : `${PRP}25`,

                border: `1px solid ${apRunning ? TXM : PRP + "60"}`,

                borderRadius: 7, color: apRunning ? TXM : PRP,

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10),

                padding: "6px 16px", cursor: apRunning ? "not-allowed" : "pointer",

                flexShrink: 0, transition: "all 0.15s", alignSelf: "flex-end",

              }}

            >{apRunning ? "Running?" : "Launch"}</button>

          </div>



          <AutopilotMonitor routerUrl={ROUTER} />

          {/* Quick task chips */}

          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: apResult ? 14 : 0 }}>

            {[

              /* P3b, 2026-10-02. A SECOND COPY of the chip set in ArchitectFanout.jsx. The
                 P3 fix replaced the old CODE prompts there and did NOT reach this file, so
                 the portal shipped two different chip sets on two surfaces and the
                 measured-bad prompts survived here. Against autopilot_graph's gate the three
                 strings this replaces did not fan out: the CSV-parser and config-validator
                 prompts both took DIRECT_ROUTE, and the REST-API one decomposed into CODE
                 legs -- and a code leg returning zero tokens triggers a ~460 s five-tier
                 retry cascade. Keep the two files in step: a chip edit in one is not a chip
                 edit in the portal. */

              { label: "Local vs hosted cost brief",
                prompt: "Write a brief comparing local inference to hosted APIs. Cover cost, operational burden, and privacy as separate sections. Prose, not code." },

              { label: "Fleet readiness assessment",
                prompt: "Write a readiness assessment of a three-node local inference fleet. Cover hardware, risks, and operations as separate sections. Prose, not code." },

              { label: "Self-hosted AI retrospective",
                prompt: "Write a short retrospective on a self-hosted AI rollout. Cover what worked, what failed, and what comes next as separate sections. Prose, not code." },

            ].map(c => (

              <button key={c.label} onClick={() => setApInput(c.prompt)} title={c.prompt} style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD,

                background: BORD, border: `1px solid ${BORDB}`,

                borderRadius: 4, padding: "3px 9px", cursor: "pointer",

              }}>{c.label}</button>

            ))}

          </div>



          {/* Result */}

          {apResult && (

            <div style={{ marginTop: 14, borderTop: `1px solid ${BORD}`, paddingTop: 14 }}>

              {apResult.error ? (

                <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(11), color: RSE }}>

                  Error: {apResult.error}

                </div>

              ) : (

                <div>

                  {/* Metadata pills */}

                  <div style={{ display: "flex", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>

                    {[

                      ["status",    apResult.bifrost?.status,                    apResult.bifrost?.status === "COMPLETE" ? GRN : AMB],

                      ["subtasks",  apResult.bifrost?.subtasks,                  BLU],

                      ["completed", apResult.bifrost?.completed,                 GRN],

                      ["failed",    apResult.bifrost?.failed,                    apResult.bifrost?.failed > 0 ? RSE : TXM],

                      ["cost",      apResult.bifrost?.cloud_cost_usd != null ? `$${apResult.bifrost.cloud_cost_usd.toFixed(4)}` : null, AMB],

                      ["elapsed",   (apResult.bifrost?.elapsed_seconds ?? apResult.elapsed_seconds) != null ? `${apResult.bifrost?.elapsed_seconds ?? apResult.elapsed_seconds}s` : null, BLU],

                    ].filter(([, v]) => v != null).map(([k, v, c]) => (

                      <div key={k} style={{

                        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5),

                        background: `${c}15`, border: `1px solid ${c}40`,

                        borderRadius: 4, padding: "2px 8px", color: c,

                      }}>{k}: {v}</div>

                    ))}

                  </div>

                  {/* Output preview */}

                  {apResult.choices?.[0]?.message?.content && (

                    <div style={{

                      background: ELEV, border: `1px solid ${BORD}`,

                      borderRadius: 6, padding: "12px 14px",

                      fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12),

                      color: TXD, lineHeight: 1.6, maxHeight: 200, overflowY: "auto",

                      whiteSpace: "pre-wrap", wordBreak: "break-word",

                    }}>

                      {apResult.choices[0].message.content.slice(0, 1200)}

                      {apResult.choices[0].message.content.length > 1200 && (

                        <span style={{ color: TXM }}> ?[truncated]</span>

                      )}

                    </div>

                  )}

                </div>

              )}

            </div>

          )}

        </Panel>



        {/* --- Profile Switcher --- */}

        <Panel>

          <Label>Profile Switcher</Label>



          {/* Bifrost */}

          <div style={{ marginBottom: 16 }}>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

              <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: GRN }}>BIFROST</span>

              <div style={{ display: "flex", alignItems: "center", gap: 5 }}>

                <PulseDot alive={bifrost?.status === "online"} size={5} />

                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>

                  {bifrost?.loaded_models?.length || 0} models

                </span>

              </div>

            </div>

            <div style={{ display: "flex", gap: 5, marginBottom: 6 }}>

              {["B-Light", "B-Dual", "B-Heavy"].map(p => {

                const active = bifrost?.profile === p;

                const c = PROFILE_C_B[p];

                return (

                  <button key={p} onClick={() => setSlashInput(`/bifrost ${p.replace("B-", "").toLowerCase()}`)}

                    style={{

                      flex: 1, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                      padding: "5px 4px", borderRadius: 4, cursor: "pointer",

                      background: active ? `${c}20` : BORD,

                      color: active ? c : TXM,

                      border: `1px solid ${active ? c + "50" : BORD}`,

                      transition: "all 0.15s",

                    }}>{p}</button>

                );

              })}

            </div>

            <div style={{

              fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace",

              background: BORD, borderRadius: 3, padding: "3px 7px",

              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

            }}>

              {bifrost?.loaded_models?.slice(0, 3).join(", ") || "?"}

            </div>

          </div>



          {/* Forge */}

          <div style={{ marginBottom: 16 }}>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

              <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: ORG }}>FORGE</span>

              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>

                <PulseDot alive={forge?.status === "online"} size={5} />

                {forge?.vram_used_bytes != null && (

                  <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>

                    {(forge.vram_used_bytes / 1e9).toFixed(1)}GB VRAM

                  </span>

                )}

              </div>

            </div>

            <div style={{ display: "flex", gap: 5, marginBottom: 6 }}>

              {["F-Multi", "F-Max"].map(p => {

                const active = forge?.profile === p;

                const c = PROFILE_C_F[p];

                return (

                  <button key={p} onClick={() => setSlashInput(`/forge ${p.replace("F-", "").toLowerCase()}`)}

                    style={{

                      flex: 1, fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),

                      padding: "5px 4px", borderRadius: 4, cursor: "pointer",

                      background: active ? `${c}20` : BORD,

                      color: active ? c : TXM,

                      border: `1px solid ${active ? c + "50" : BORD}`,

                      transition: "all 0.15s",

                    }}>{p}</button>

                );

              })}

            </div>

            <div style={{

              fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace",

              background: BORD, borderRadius: 3, padding: "3px 7px",

              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

            }}>

              {forge?.loaded_models?.slice(0, 2).join(", ") || "?"}

            </div>

          </div>



          {/* Hearth Vega8 */}

          <div>

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>

              <span style={{ fontFamily: "Instrument Sans, sans-serif", fontSize: fs(10), color: BLU }}>VEGA 8</span>

              <PulseDot alive={vega8?.status === "online"} size={5} />

            </div>

            <div style={{

              fontSize: fs(8.5), color: TXM, fontFamily: "'JetBrains Mono', monospace",

              background: BORD, borderRadius: 3, padding: "3px 7px",

              whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",

            }}>

              {vega8?.loaded_models?.slice(0, 3).join(", ") || "?"}

            </div>

          </div>

        </Panel>



        {/* --- Slash Commands --- */}

        <Panel>

          <Label>Slash Commands</Label>



          {/* Quick chips */}

          <div style={{ display: "flex", flexWrap: "wrap", gap: 5, marginBottom: 12 }}>

            {SLASH_CMDS.filter(([cmd]) => isAdmin || !cmd.startsWith("/privacy")).map(([cmd, desc]) => (

              <button key={cmd} onClick={() => handleSlashCmd(cmd)} title={desc} style={{

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD,

                background: BORD, border: `1px solid ${BORDB}`,

                borderRadius: 4, padding: "3px 8px", cursor: "pointer",

                transition: "all 0.12s",

              }}>{cmd}</button>

            ))}

          </div>



          {/* Input */}

          <div style={{

            display: "flex", gap: 7, marginBottom: 10,

            background: ELEV, border: `1px solid ${slashRunning ? BLU + "60" : BORDB}`,

            borderRadius: 6, padding: "7px 10px", transition: "border-color 0.2s",

          }}>

            <input

              value={slashInput}

              onChange={e => setSlashInput(e.target.value)}

              onKeyDown={e => { if (e.key === "Enter") handleSlashCmd(slashInput); }}

              placeholder="/command ?"

              disabled={slashRunning}

              style={{

                flex: 1, background: "transparent", border: "none",

                color: TX, fontFamily: "'JetBrains Mono', monospace",

                fontSize: fs(12), outline: "none",

              }}

            />

            <button

              onClick={handleSlashCmd.bind(null, slashInput)}

              disabled={!slashInput.trim() || slashRunning}

              style={{

                background: slashRunning ? "transparent" : `${BLU}20`,

                border: `1px solid ${slashRunning ? TXM : BLU + "50"}`,

                borderRadius: 5, color: slashRunning ? TXM : BLU,

                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9),

                padding: "4px 12px", cursor: slashRunning ? "not-allowed" : "pointer",

                flexShrink: 0, transition: "all 0.15s",

              }}

            >{slashRunning ? "?" : "Run"}</button>

          </div>



          {/* Result */}

          {slashResult && (

            <div style={{

              background: ELEV, border: `1px solid ${BORD}`,

              borderRadius: 5, padding: "10px 12px",

              fontFamily: "'JetBrains Mono', monospace", fontSize: fs(11),

              color: TXD, lineHeight: 1.6, maxHeight: 200, overflowY: "auto",

              whiteSpace: "pre-wrap", wordBreak: "break-word",

            }}>

              {slashResult}

            </div>

          )}

        </Panel>



      </div>

    </div>

  );

}

export default CommandSurface;
