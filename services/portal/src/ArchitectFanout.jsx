import React, { useState, useRef, useCallback } from "react";

// Shared primitives still live in BifrostPortal.jsx. They are hoisted
// `function` declarations, so this back-import forms a safe ES-module cycle
// (every reference below is evaluated at render time, long after both modules
// finish loading). Same convention CommandSurface.jsx uses.
import { Panel, Label, PulseDot } from "./BifrostPortal";
import { SURF, ELEV, BORD, BORDB, TX, TXD, TXM, GRN, BLU, AMB, ORG, RSE, PRP,
  fs,
} from "./theme/tokens";

// ============================================================
// CONFIG / DESIGN TOKENS
// Re-declared from BifrostPortal.jsx (Phase 1: the palette now comes from ./theme/tokens).
// Keep in sync with the source-of-truth definitions at the top of
// BifrostPortal.jsx / CommandSurface.jsx.
// ============================================================
const ROUTER = "/router";

// Palette moved to ./theme/tokens.js (Phase 1); imported above.

const POLL_INTERVAL_MS = 3000;
const WALL_TIMEOUT_SEC = 900; // client-side safety cap

// 2.2-a: the task input cap, from rev4 D2 (20,000 characters). ONE constant, because the
// ArchitectSurface this replaces repeated the number in three places -- the textarea's maxLength,
// the counter text, and a warning threshold set by eye -- which is three places to drift apart.
// TASK_WARN_CHARS is not specified by rev4; it is carried over from that dead copy (18000) so the
// behaviour is preserved rather than reinvented.
const TASK_MAX_CHARS  = 20000;
const TASK_WARN_CHARS = 18000;

// Terminal states returned by the autopilot poll endpoint.
// PARTIAL (router v220): the run finished but some subtasks failed. It carries a real
// assembled_output and /result serves it with 200 exactly like COMPLETE -- main.py only
// special-cases QUEUED/RUNNING (202) and FAILED (500). If PARTIAL is missing from this
// list the poll loop never exits and burns down to WALL_TIMEOUT_SEC, rendering TIMEOUT
// and never fetching the output.
// FAILED_NEEDS_HUMAN: the router has always been able to emit this (a decompose that
// exhausts its REPLAN returns it). It was absent here, so such runs already mis-rendered
// as TIMEOUT after a 900s spin.
const TERMINAL = ["COMPLETE", "PARTIAL", "FAILED", "FAILED_NEEDS_HUMAN", "ERROR", "CANCELLED", "TIMEOUT"];

// Statuses that carry a usable assembled_output worth fetching from /result.
const HAS_RESULT = ["COMPLETE", "PARTIAL"];

// Normalize the many status spellings the fleet emits into a color.
function statusColor(s) {
  const v = String(s || "").toUpperCase();
  if (["COMPLETE", "PASS", "SUCCESS", "DONE"].includes(v)) return GRN;
  // PARTIAL is amber, deliberately: it produced output, but it is NOT a pass. Colouring
  // a 1-of-3 run green is the failure mode this whole change exists to remove.
  if (["RUNNING", "PENDING", "QUEUED", "DISPATCHED", "PARTIAL"].includes(v)) return AMB;
  if (["FAILED", "FAIL", "FAILED_NEEDS_HUMAN", "ERROR", "CANCELLED", "TIMEOUT"].includes(v)) return RSE;
  return TXM;
}

// Tier badge color — local tiers green-ish, cloud tiers purple-ish.
function tierColor(tier) {
  const t = String(tier || "");
  if (t.startsWith("3-")) return PRP;
  if (t.startsWith("2-")) return BLU;
  if (t.startsWith("1-")) return GRN;
  return TXD;
}

// Small reusable pill.
function Pill({ k, v, c }) {
  return (
    <div style={{
      fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8.5),
      background: `${c}15`, border: `1px solid ${c}40`,
      borderRadius: 4, padding: "2px 8px", color: c,
    }}>{k}: {v}</div>
  );
}

// One subtask leg row. Mirrors AutopilotMonitor's expanded-leg rendering but
// shows the richer per-leg fields (tier, tokens, cost) the fan-out exposes.
function LegRow({ sid, leg }) {
  const c = statusColor(leg?.status);
  const tc = tierColor(leg?.tier);
  return (
    <div style={{
      display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
      background: ELEV, border: `1px solid ${BORD}`,
      borderRadius: 6, padding: "6px 10px",
    }}>
      <span style={{
        width: 6, height: 6, borderRadius: "50%",
        background: c, flexShrink: 0,
        boxShadow: statusColor(leg?.status) === AMB ? `0 0 6px ${AMB}` : "none",
      }} />
      <span style={{
        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9), color: TX,
        minWidth: 120,
      }}>{String(sid).slice(0, 24)}</span>
      {leg?.tier != null && (
        <span style={{
          fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: tc,
          background: `${tc}12`, border: `1px solid ${tc}33`,
          borderRadius: 3, padding: "1px 6px",
        }}>{leg.tier}</span>
      )}
      {leg?.tokens != null && (
        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD }}>
          {leg.tokens} tok
        </span>
      )}
      {leg?.cost_usd != null && (
        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: AMB }}>
          ${Number(leg.cost_usd).toFixed(4)}
        </span>
      )}
      <span style={{
        marginLeft: "auto",
        fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: c,
      }}>{leg?.status || "pending"}</span>
    </div>
  );
}

function ArchitectFanoutSurface() {
  const [input, setInput]       = useState("");
  const [running, setRunning]   = useState(false);
  const [graphId, setGraphId]   = useState(null);
  const [poll, setPoll]         = useState(null);   // latest poll payload
  const [result, setResult]     = useState(null);   // final assembled payload
  const [error, setError]       = useState(null);
  const cancelRef = useRef(false);

  const subtaskProgress = poll?.subtask_progress || {};
  const legKeys = Object.keys(subtaskProgress);
  const total = poll?.subtasks_total || legKeys.length || 0;
  const completed = poll?.subtasks_completed != null
    ? poll.subtasks_completed
    : legKeys.filter(k => statusColor(subtaskProgress[k]?.status) === GRN).length;
  const failed = poll?.subtasks_failed || 0;
  const pct = total ? Math.round((completed / total) * 100) : 0;
  const status = result?.status || poll?.status || (running ? "QUEUED" : null);

  const handleSubmit = useCallback(async () => {
    const text = input.trim();
    if (!text || running) return;
    cancelRef.current = false;
    setRunning(true);
    setError(null);
    setPoll(null);
    setResult(null);
    setGraphId(null);

    try {
      // 1. Submit the NL task to the autopilot fan-out.
      const submitResp = await fetch(`${ROUTER}/autopilot/submit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: text }),
      });
      if (!submitResp.ok) throw new Error(`submit HTTP ${submitResp.status}`);
      const submit = await submitResp.json();

      const pollUrl = submit.poll_url;
      const resultUrl = submit.result_url;
      if (!pollUrl || !resultUrl) throw new Error("submit missing poll_url/result_url");
      setGraphId(submit.graph_id || null);

      // 2. Poll until a terminal state (or wall-clock cap).
      const started = Date.now();
      let cur = submit.status || "QUEUED";
      let latest = submit;
      while (!TERMINAL.includes(String(cur).toUpperCase())) {
        if (cancelRef.current) { cur = "CANCELLED"; break; }
        await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
        if (cancelRef.current) { cur = "CANCELLED"; break; }
        try {
          const pr = await fetch(`${ROUTER}${pollUrl}`);
          // poll endpoint may 202 while QUEUED/RUNNING — still try to parse.
          latest = await pr.json();
        } catch {
          continue; // transient poll hiccup — retry next tick
        }
        cur = latest.status || cur;
        setPoll(latest);
        if ((Date.now() - started) / 1000 > WALL_TIMEOUT_SEC) {
          cur = "TIMEOUT";
          break;
        }
      }

      // 3. On COMPLETE or PARTIAL, fetch the assembled result. result_url returns 202
      //    while still QUEUED/RUNNING, so guard on the HTTP status.
      if (HAS_RESULT.includes(String(cur).toUpperCase()) && !cancelRef.current) {
        const rr = await fetch(`${ROUTER}${resultUrl}`);
        if (rr.status === 202) {
          // Backend says still running despite COMPLETE poll — keep poll view.
          setResult({ ...latest, status: "RUNNING", graph_id: submit.graph_id });
        } else if (rr.ok) {
          const data = await rr.json();
          // Keep the REAL status. This used to hardcode "COMPLETE", which would relabel
          // a PARTIAL run green on arrival and undo the whole point of the status fix.
          setResult({ ...data, status: data.status || cur, graph_id: submit.graph_id });
        } else {
          throw new Error(`result HTTP ${rr.status}`);
        }
      } else {
        setResult({ ...latest, status: cur, graph_id: submit.graph_id });
      }
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setRunning(false);
    }
  }, [input, running]);

  const handleCancel = useCallback(() => {
    cancelRef.current = true;
    setRunning(false);
  }, []);

  const assembled = result?.assembled_output;

  return (
    <div style={{ flex: 1, overflowY: "auto", padding: "18px 20px" }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12, maxWidth: 1100 }}>

        {/* --- Task input --- */}
        <Panel span="1 / -1" style={{ borderColor: `${PRP}35`, background: `linear-gradient(140deg, ${SURF} 0%, ${PRP}08 100%)` }}>
          <Label>Fan-out: Autopilot</Label>
          <div style={{
            display: "flex", gap: 8, marginBottom: 10,
            background: ELEV, border: `1px solid ${running ? PRP + "60" : BORDB}`,
            borderRadius: 8, padding: "10px 14px", transition: "border-color 0.2s",
          }}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleSubmit(); } }}
              placeholder="Describe a task to decompose and fan out across the fleet…  (Cmd/Ctrl+Enter to submit)"
              disabled={running}
              rows={3}
              maxLength={TASK_MAX_CHARS}
              style={{
                flex: 1, background: "transparent", border: "none",
                color: TX, fontFamily: "Instrument Sans, sans-serif",
                fontSize: fs(13), lineHeight: 1.5, resize: "vertical", outline: "none",
              }}
            />
            <div style={{ display: "flex", flexDirection: "column", gap: 6, justifyContent: "flex-end" }}>
              <button
                onClick={handleSubmit}
                disabled={!input.trim() || running}
                style={{
                  background: running ? "transparent" : `${PRP}25`,
                  border: `1px solid ${running ? TXM : PRP + "60"}`,
                  borderRadius: 7, color: running ? TXM : PRP,
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10),
                  padding: "6px 16px", cursor: running ? "not-allowed" : "pointer",
                  flexShrink: 0, transition: "all 0.15s",
                }}
              >{running ? "Running…" : "Submit"}</button>
              {running && (
                <button
                  onClick={handleCancel}
                  style={{
                    background: "transparent", border: `1px solid ${RSE}50`,
                    borderRadius: 7, color: RSE,
                    fontFamily: "Instrument Sans, sans-serif", fontSize: fs(9),
                    padding: "4px 16px", cursor: "pointer",
                  }}
                >Cancel</button>
              )}
            </div>
          </div>

          {/* 2.2-a: character counter. maxLength on its own is a SILENT cap -- it simply stops
              accepting keystrokes with no feedback, on a field whose whole purpose is a long brief.
              This makes the limit visible before it bites, and warns at TASK_WARN_CHARS the way the
              ArchitectSurface being retired did. */}
          <div style={{
            display: "flex", justifyContent: "flex-end", marginBottom: 10,
            fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8),
            color: input.length > TASK_WARN_CHARS ? AMB : TXM,
          }}>
            {input.length} / {TASK_MAX_CHARS} chars
          </div>

          {/* Quick task chips */}
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {[
              /* P3, 2026-10-02. The previous three chips were CODE prompts, and MEASURED
                 against autopilot_graph's gate two of them did not fan out at all:
                   "Write a Python CSV parser with error handling"  DIRECT_ROUTE, 0 keywords
                   "Build a REST API health check module"           decomposed -- but into
                                                                   CODE legs (build/api/module
                                                                   = 3 matches, so matched>=3)
                   "Create a config validator with JSON schema"    DIRECT_ROUTE, 1 keyword
                 So "none of them fan out" was true of two of three; the third fanned out the
                 WRONG WAY. A code leg returning zero tokens triggers a ~460 s five-tier retry
                 cascade, which is why the demo excludes code legs from fan-out until D1c.

                 These three are prose DOCUMENT tasks: _is_document_task() is true for each, no
                 _CODE_SIGNALS word appears in any of them, and each gates to
                 "Document task detected: section fan-out required." -- the same path the Act 4
                 prompt takes. Each names three sections, the strongest available nudge toward a
                 3-leg fan-out; the leg COUNT is still not deterministic (the decomposer is an
                 MoE that returned 3 then 1 from identical input at temperature 0), so only the
                 `legs` parameter pins it.

                 label is what the chip shows, prompt is what it loads: a prompt long enough to
                 name three sections is too long to read as a button. */
              { label: "Local vs hosted cost brief",
                prompt: "Write a brief comparing local inference to hosted APIs. Cover cost, operational burden, and privacy as separate sections. Prose, not code." },
              { label: "Fleet readiness assessment",
                prompt: "Write a readiness assessment of a three-node local inference fleet. Cover hardware, risks, and operations as separate sections. Prose, not code." },
              { label: "Self-hosted AI retrospective",
                prompt: "Write a short retrospective on a self-hosted AI rollout. Cover what worked, what failed, and what comes next as separate sections. Prose, not code." },
            ].map(c => (
              <button key={c.label} onClick={() => setInput(c.prompt)} title={c.prompt} disabled={running} style={{
                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXD,
                background: BORD, border: `1px solid ${BORDB}`,
                borderRadius: 4, padding: "3px 9px",
                cursor: running ? "not-allowed" : "pointer",
              }}>{c.label}</button>
            ))}
          </div>
        </Panel>

        {/* --- Error --- */}
        {error && (
          <Panel span="1 / -1" style={{ borderColor: `${RSE}50` }}>
            <Label style={{ color: RSE }}>Error</Label>
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(11), color: RSE }}>
              {error}
            </div>
          </Panel>
        )}

        {/* --- Progress + legs --- */}
        {(running || poll || result) && (
          <Panel span="1 / -1" style={{ borderColor: `${BLU}30` }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
              <Label style={{ margin: 0 }}>Fan-out Progress</Label>
              {graphId && (
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: fs(8), color: TXM }}>
                  {String(graphId).slice(0, 12)}
                </span>
              )}
              <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
                <PulseDot alive={running} size={5} />
                <span style={{
                  fontFamily: "'JetBrains Mono', monospace", fontSize: fs(9),
                  color: statusColor(status),
                  background: `${statusColor(status)}15`,
                  border: `1px solid ${statusColor(status)}40`,
                  borderRadius: 4, padding: "2px 8px",
                }}>{String(status || "—").toUpperCase()}</span>
              </div>
            </div>

            {/* Overall pills */}
            <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
              <Pill k="legs" v={`${completed}/${total || "?"}`} c={BLU} />
              <Pill k="progress" v={`${pct}%`} c={pct === 100 ? GRN : AMB} />
              {failed > 0 && <Pill k="failed" v={failed} c={RSE} />}
              {(result?.elapsed_seconds ?? poll?.elapsed_seconds) != null && (
                <Pill k="elapsed" v={`${result?.elapsed_seconds ?? poll?.elapsed_seconds}s`} c={BLU} />
              )}
              {(result?.cloud_cost_usd ?? poll?.cloud_cost_usd) != null && (
                <Pill k="cost" v={`$${Number(result?.cloud_cost_usd ?? poll?.cloud_cost_usd).toFixed(4)}`} c={AMB} />
              )}
              {(result?.human_review_required ?? poll?.human_review_required) && (
                <Pill k="review" v="required" c={ORG} />
              )}
            </div>

            {/* Progress bar */}
            <div style={{ height: 4, background: BORD, borderRadius: 2, overflow: "hidden", marginBottom: 14 }}>
              <div style={{
                width: `${pct}%`, height: "100%",
                background: pct === 100 ? GRN : AMB,
                transition: "width 0.3s ease",
              }} />
            </div>

            {/* Per-leg rows */}
            {legKeys.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {legKeys.map(sid => (
                  <LegRow key={sid} sid={sid} leg={subtaskProgress[sid]} />
                ))}
              </div>
            ) : (
              <div style={{
                fontFamily: "'JetBrains Mono', monospace", fontSize: fs(10), color: TXD,
                padding: "8px 0",
              }}>
                {running ? "Decomposing task and dispatching legs…" : "No subtask legs reported."}
              </div>
            )}
          </Panel>
        )}

        {/* --- Assembled result --- */}
        {assembled != null && (
          <Panel span="1 / -1" style={{ borderColor: `${GRN}35` }}>
            <Label style={{ color: GRN }}>Assembled Result</Label>
            <div style={{
              background: ELEV, border: `1px solid ${BORD}`,
              borderRadius: 6, padding: "12px 14px",
              fontFamily: "Instrument Sans, sans-serif", fontSize: fs(12),
              color: TX, lineHeight: 1.6, maxHeight: 420, overflowY: "auto",
              whiteSpace: "pre-wrap", wordBreak: "break-word",
            }}>
              {typeof assembled === "string" ? assembled : JSON.stringify(assembled, null, 2)}
            </div>
          </Panel>
        )}

      </div>
    </div>
  );
}

export default ArchitectFanoutSurface;
