#!/usr/bin/env python3
"""
langfuse_trace_reader.py -- SIA Phase 2A. Reads full per-cycle trajectories (NOT flat
decisions.md summaries) and maps each trace + proposed policy.md diff to the 8-dim binary
quality vector x1..x8 consumed by quantum_reward.py.

Primary source: Langfuse v2 (GET /api/public/traces), env-gated like langfuse_client.py
(LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST). Basic auth, never logged.
Fallback (keys absent OR Langfuse empty): inference_events.db (INFERENCE_EVENTS_DB env or
the in-pod default). x7/x8 use the working-hours/overnight HEURISTIC when source is untagged
(99.96% untagged as of 2026-06-27) -- output is flagged HEURISTIC and the caller-tagging
brief is owed for EXACT attribution.

CLI:  python langfuse_trace_reader.py --tail 5 --summary
"""
import argparse
import base64
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import httpx

# Protected files whose gates must not be widened (x6). Mirrors the orch_apply set.
PROTECTED_FILES = (
    "igor_tool_loop.py", "main.py", "config.py", "fleet_config.json",
    "policy_instructions.py", "bifrost_shell.py", "bifrost_cycle.py",
    "session_o_gates.py",
)
# WRITE/EXECUTE class promotion markers (x5): a diff that adds these to an allowed/READ set.
WRITE_EXEC_MARKERS = ("write_file", "run_python", "run_shell", "execute", "WRITE", "EXECUTE", "delete")

DEFAULT_DB = os.environ.get(
    "INFERENCE_EVENTS_DB", "/app/logs/v120-telemetry/inference_events.db"
)


@dataclass
class TraceRecord:
    timestamp: str = ""
    model: str = ""
    tier_used: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    accepted: bool = None  # None when unknown
    source: str = ""       # cc_dispatch | session_o | "" (untagged)
    tool_calls: list = field(default_factory=list)   # [{name, is_error}]
    tool_violations: list = field(default_factory=list)


class LangfuseTraceReader:
    def __init__(self):
        self.pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        self.sk = os.environ.get("LANGFUSE_SECRET_KEY")
        self.host = (os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
        self.db_path = DEFAULT_DB
        self.via = None  # "langfuse" | "db" -- set after get_recent_traces

    def _enabled(self) -> bool:
        return bool(self.pk and self.sk and self.host)

    # -- primary: Langfuse v2 --------------------------------------------------
    def _langfuse_traces(self, n_cycles, source):
        auth = base64.b64encode(f"{self.pk}:{self.sk}".encode()).decode()
        params = {"limit": n_cycles, "orderBy": "timestamp.desc"}
        with httpx.Client(timeout=8.0) as client:
            r = client.get(
                f"{self.host}/api/public/traces",
                params=params,
                headers={"Authorization": f"Basic {auth}"},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
        out = []
        for t in data:
            meta = t.get("metadata") or {}
            src = (meta.get("source") or "").strip()
            if src in ("untagged", "none", "None"):
                src = ""
            if source and src != source:
                continue
            obs = t.get("observations") or []
            tool_calls, viol = [], []
            for o in obs:
                om = o.get("metadata") or {}
                if om.get("tool_name"):
                    is_err = bool(om.get("is_error") or om.get("error"))
                    tool_calls.append({"name": om["tool_name"], "is_error": is_err})
                    if om.get("breach") or om.get("violation"):
                        viol.append(om.get("breach") or om.get("violation"))
            out.append(TraceRecord(
                timestamp=t.get("timestamp", ""),
                model=(meta.get("model") or t.get("name") or ""),
                tier_used=str(meta.get("tier") or meta.get("tier_used") or ""),
                latency_ms=float(t.get("latency") or 0.0),
                source=src,
                tool_calls=tool_calls,
                tool_violations=viol,
            ))
        return out

    # -- fallback: inference_events.db ----------------------------------------
    def _db_traces(self, n_cycles, source):
        if not os.path.exists(self.db_path):
            return []
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(inference_events)")}
        sel = [c for c in ("timestamp", "model", "tier_used", "latency_ms", "status",
                            "source", "tool_calls_made", "tool_successful_calls",
                            "tool_iterations") if c in cols]
        rows = con.execute(
            f"SELECT {','.join(sel)} FROM inference_events ORDER BY rowid DESC LIMIT ?",
            (n_cycles,),
        ).fetchall()
        con.close()
        out = []
        for row in rows:
            d = dict(row)
            src = (d.get("source") or "").strip()
            if src in ("None", "none", "untagged"):
                src = ""
            # HEURISTIC source split when untagged: overnight 02-04 -> session_o, else cc_dispatch
            if not src:
                src = self._heuristic_source(d.get("timestamp", ""))
            if source and src != source:
                continue
            # sqlite may return these columns as TEXT; coerce so range()/comparison work.
            try:
                made = int(d.get("tool_calls_made") or 0)
            except (TypeError, ValueError):
                made = 0
            try:
                ok = int(d.get("tool_successful_calls") or 0)
            except (TypeError, ValueError):
                ok = 0
            out.append(TraceRecord(
                timestamp=str(d.get("timestamp", "")),
                model=str(d.get("model", "")),
                tier_used=str(d.get("tier_used", "")),
                latency_ms=float(d.get("latency_ms") or 0.0),
                source=src,
                tool_calls=[{"name": "tool", "is_error": i >= ok} for i in range(made)],
            ))
        return out

    @staticmethod
    def _heuristic_source(ts):
        """Working-hours heuristic: overnight 02:00-04:00 -> session_o; else cc_dispatch.
        HEURISTIC, not EXACT -- owed: caller-side X-Bifrost-Source tagging brief."""
        try:
            hour = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).hour
        except Exception:
            return "cc_dispatch"
        return "session_o" if 2 <= hour < 5 else "cc_dispatch"

    def get_recent_traces(self, n_cycles=30, source=None):
        if self._enabled():
            try:
                traces = self._langfuse_traces(n_cycles, source)
                if traces:
                    self.via = "langfuse"
                    return traces
            except Exception:
                pass
        self.via = "db"
        return self._db_traces(n_cycles, source)

    # -- x-vector mapping ------------------------------------------------------
    def compute_x_vector(self, traces, proposed_diff, baseline=None):
        """Map a window of traces + a proposed policy.md diff -> [x1..x8] (1=good).
        baseline: optional dict of prior-generation metrics for x1/x3/x7/x8 comparison."""
        baseline = baseline or {}
        cur = self._window_metrics(traces)

        x1 = int(cur["accept_rate"] >= baseline.get("accept_rate", 0.0))
        x2 = int(cur["tool_error_rate"] <= baseline.get("tool_error_rate", 1.0))
        x3 = int(cur["p95_latency_ms"] <= baseline.get("p95_latency_ms", float("inf")))
        x4 = int(not self._has_new_breach(traces, baseline.get("breach_taxonomy", set())))
        # x5/x6 = pure static analysis of the proposed diff (no model call, no trace).
        x5 = int(not self._promotes_write_exec(proposed_diff))
        x6 = int(not self._widens_protected_gate(proposed_diff))
        cc = self._window_metrics([t for t in traces if t.source == "cc_dispatch"])
        so = self._window_metrics([t for t in traces if t.source == "session_o"])
        x7 = int(cc["accept_rate"] >= baseline.get("cc_accept_rate", 0.0))
        x8 = int(so["accept_rate"] >= baseline.get("so_accept_rate", 0.0))
        return [x1, x2, x3, x4, x5, x6, x7, x8]

    @staticmethod
    def _window_metrics(traces):
        n = len(traces) or 1
        acc = [t for t in traces if t.accepted is True]
        total_calls = sum(len(t.tool_calls) for t in traces) or 1
        errs = sum(1 for t in traces for c in t.tool_calls if c.get("is_error"))
        lats = sorted(t.latency_ms for t in traces) or [0.0]
        p95 = lats[min(len(lats) - 1, int(0.95 * len(lats)))]
        return {
            "n": len(traces),
            "accept_rate": len(acc) / n if any(t.accepted is not None for t in traces) else 0.0,
            "tool_error_rate": errs / total_calls,
            "p95_latency_ms": p95,
        }

    @staticmethod
    def _has_new_breach(traces, known):
        seen = {v for t in traces for v in t.tool_violations}
        return bool(seen - set(known))

    @staticmethod
    def _promotes_write_exec(diff):
        added = [ln for ln in (diff or "").splitlines() if ln.startswith("+")]
        return any(any(m in ln for m in WRITE_EXEC_MARKERS) for ln in added)

    @staticmethod
    def _widens_protected_gate(diff):
        added = [ln for ln in (diff or "").splitlines() if ln.startswith("+")]
        return any(any(p in ln for p in PROTECTED_FILES) for ln in added)


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=5)
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--source", default=None, choices=[None, "cc_dispatch", "session_o"])
    args = ap.parse_args()
    rdr = LangfuseTraceReader()
    traces = rdr.get_recent_traces(n_cycles=args.tail, source=args.source)
    heuristic = rdr.via == "db"
    if args.summary:
        m = rdr._window_metrics(traces)
        print(json.dumps({
            "via": rdr.via,
            "source_attribution": "HEURISTIC (caller-tagging owed)" if heuristic else "exact",
            "n_traces": len(traces),
            "accept_rate": round(m["accept_rate"], 3),
            "tool_error_rate": round(m["tool_error_rate"], 3),
            "p95_latency_ms": round(m["p95_latency_ms"], 1),
            "sample": [asdict(t) for t in traces[:3]],
        }, indent=2, default=str))
    else:
        for t in traces:
            print(json.dumps(asdict(t), default=str))


if __name__ == "__main__":
    _main()
