"""
telemetry.py -- BIFROST InferenceEvent schema + async SQLite writer
No prompt/completion content stored. Non-blocking writes via thread pool.
"""
import asyncio
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from pydantic import BaseModel, Field

# -- Host identity ------------------------------------------------------------
# CORRECTED 2026-09-09 (telemetry item 4). These two were module-level literals:
#     MACHINE  = "bifrost"
#     HARDWARE = "bifrost-3950x-9070xt-rdna4"
# so every row of every table in this DB claimed Bifrost. Measured on the live DB
# (/app/logs/v120-telemetry/inference_events.db) BEFORE this change -- exactly one
# distinct value per table, no exceptions:
#     inference_events    43,300 / 43,300   machine='bifrost'
#     routing_decisions   41,678 / 41,678
#     embed_events        86,042 / 86,042
#     pipeline_stages         99 /     99
# and it is wrong even about its own writer: the router pod that emits these rows
# runs in the k3d cluster whose API server is https://<lan-host>:49442, and
# <lan-host> is HEARTH. This column has never once named the host that wrote the row.
#
# `machine` means THE HOST THAT WROTE THE ROW. It is deliberately NOT the host that
# served the inference -- that question is answered by tier_used joined to
# fleet_config.json tiers[].machine, and conflating the two is how this column became
# useless. A row with machine='hearth' and tier_used='2.5' is a Hearth-hosted router
# recording a Forge-served request, and both halves of that are now true.
#
# Vocabulary is fleet_config.json's own tiers[].machine domain -- nothing invented
# here. fleet_config also uses 'cloud', which cannot appear below: a cloud provider
# never runs this process.
#
# Resolution order, first hit wins:
#   1. BIFROST_MACHINE env var. The ONLY mechanism that works in a container:
#      socket.gethostname() inside the router pod returns the POD name (measured:
#      'bifrost-router-77fbbbb584-jbjtm'), which names no machine and changes on
#      every redeploy.
#   2. socket.gethostname(), normalized against the fleet hostnames measured
#      2026-09-09 by running `hostname` on each box.
#   3. MACHINE_UNKNOWN -- never a silent "bifrost". An unattributable row must say so
#      rather than blame Bifrost, which is the defect being fixed.
MACHINE_UNKNOWN = "unknown"

def _fleet_hostnames_from_env():
    """host -> fleet label, from BIFROST_FLEET_HOSTNAMES. Empty when unset.

    Format: `hostA=bifrost,hostB=hearth`. Hosts are lower-cased because the lookup below
    lower-cases what socket.gethostname() returns, and Windows reports a hostname in
    whatever case it was typed at install time.

    THE HOSTNAMES ARE NOT COMMITTED. This table used to spell two real machine names in
    shipping code. A fleet label is this project's vocabulary and belongs in the source; a
    hostname names a box on somebody's network and does not.
    """
    out = {}
    for pair in os.environ.get("BIFROST_FLEET_HOSTNAMES", "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        host, machine = pair.split("=", 1)
        host, machine = host.strip().lower(), machine.strip().lower()
        if host and machine:
            out[host] = machine
    return out


_FLEET_HOSTNAMES = _fleet_hostnames_from_env()

# COMMITTED, and deliberately no longer derived from the table above. These are the fleet's
# own labels, they appear throughout this release already, and three tests plus the
# BIFROST_MACHINE validation below depend on the set being stable. Deriving it from an
# env-driven table would make it empty whenever the table is unset -- which would turn the
# validation at `declared in _FLEET_MACHINES` into a check that rejects everything.
_FLEET_MACHINES = frozenset({"bifrost", "hearth", "forge"})

# fleet_config.json carries hardware PER TIER, not per machine: 'hearth' alone spans
# four distinct hardware strings ('RX 5700 XT 8GB RDNA1', 'Vega 8 iGPU', 'Hearth
# Docker', 'RX 5700 XT 8GB (co-resident ...)'). There is therefore no fleet source of
# record for a machine-level hardware string, and writing one here would invent the
# third vocabulary this fix exists to avoid. Only Bifrost's is on record -- it is the
# old constant, kept verbatim so bifrost-written rows stay comparable across the
# cutover. Every other host reports the sentinel until a per-machine hardware source
# of record exists.
HARDWARE_UNKNOWN = "unknown"
_FLEET_HARDWARE = {"bifrost": "bifrost-3950x-9070xt-rdna4"}


def resolve_machine() -> str:
    """Fleet short name of the host running this process, or MACHINE_UNKNOWN.

    Never guesses and never falls back to a machine name: the caller gets either a
    name that was actually established or an explicit sentinel.
    """
    declared = (os.environ.get("BIFROST_MACHINE") or "").strip().lower()
    if declared:
        if declared in _FLEET_MACHINES:
            return declared
        logging.warning(
            "[telemetry] BIFROST_MACHINE=%r is not a fleet machine name %s -- "
            "recording machine=%r", declared, sorted(_FLEET_MACHINES), MACHINE_UNKNOWN)
        return MACHINE_UNKNOWN
    try:
        host = socket.gethostname() or ""
    except Exception:
        host = ""
    short = host.split(".")[0].strip().lower()
    if short in _FLEET_HOSTNAMES:
        return _FLEET_HOSTNAMES[short]
    logging.warning(
        "[telemetry] hostname %r maps to no fleet machine (inside a container this is "
        "the pod name, not a host) -- recording machine=%r. Set BIFROST_MACHINE to one "
        "of %s to attribute these rows.", host, MACHINE_UNKNOWN, sorted(_FLEET_MACHINES))
    return MACHINE_UNKNOWN


def resolve_hardware(machine: str) -> str:
    """Hardware string for an already-resolved machine, or HARDWARE_UNKNOWN."""
    return _FLEET_HARDWARE.get(machine, HARDWARE_UNKNOWN)


MACHINE  = resolve_machine()
HARDWARE = resolve_hardware(MACHINE)

# Path resolution: env var > OS default.
# k3d (Linux): set BIFROST_TELEMETRY_DIR=/data/bifrost-logs/telemetry in k8s manifest.
# Windows dev: falls back to F:\ SMB share (bifrost-logs). Writes fail gracefully if unavailable.
import os as _os
_default_tel = (
    "/data/bifrost-logs/telemetry"
    if _os.name == "posix"
    else r"F:\bifrost-logs\telemetry"
)
TELEMETRY_DIR = Path(_os.environ.get("BIFROST_TELEMETRY_DIR", _default_tel))
TELEMETRY_DB  = TELEMETRY_DIR / "inference_events.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS inference_events (
    event_id              TEXT PRIMARY KEY,
    timestamp             REAL,
    machine               TEXT,
    hardware              TEXT,
    strategy              TEXT,
    complexity_band       TEXT,
    tier_used             TEXT,
    tier_history          TEXT,
    escalation_count      INTEGER,
    model                 TEXT,
    tokens_input          INTEGER,
    tokens_output         INTEGER,
    tokens_total          INTEGER,
    latency_ms            REAL,
    cloud_cost_usd        REAL,
    -- CAREFUL (2026-09-07 audit): this is the EXECUTE-phase status, emitted before
    -- verification runs, and hardcoded "PASS" at the autopilot emit site. It means
    -- "the model returned text", NOT "the leg passed": 3,109 PASS / 2 FAIL over three
    -- months, and on the first joined bank run all 17 rows read PASS while 9 of them
    -- had FAILED verification. For the real verdict use verify_status below.
    status                TEXT,
    -- v127 telemetry-wire-in additions (2026-05-23): classifier decision signal.
    -- Note: complexity_band already captures the FINAL band. These add the upstream
    -- per-request classifier detail (which path won, its confidence, rule scores,
    -- reasoning, hint usage) so misclass / threshold analysis can run from the DB.
    -- Overlaps partially with routing_decisions. NB (2026-09-07 audit): this table has
    -- NO request_id column and never has -- PRAGMA table_info returned 27 columns with
    -- no request_id -- so the "joinable on request_id" this comment used to claim was
    -- never possible. v416 added graph_id + subtask_id below, which IS a real join key
    -- for AUTOPILOT rows; INTERACTIVE rows still have none, so the duplicated fields
    -- here remain the only way to analyze those without a join.
    classifier_source     TEXT,    -- npu | rule | fallback | hint | (empty: legacy row)
    classifier_confidence REAL,    -- final ClassificationResult.confidence (winner's conf)
    classifier_scores     TEXT,    -- JSON of per-band feature scores (rule path); "{}" on NPU
    classifier_reasoning  TEXT,    -- ClassificationResult.reasoning
    hint_override         INTEGER, -- 0/1: X-Complexity-Hint short-circuited the classify
    -- igor-metrics (2026-06-19): Igor tool-loop invocation telemetry from
    -- response._bifrost_tool_loop (run_tool_loop). Empty/0 on non-tool-loop rows.
    tool_calls_made       TEXT,    -- JSON list of tools actually executed (tools_called)
    tool_successful_calls INTEGER, -- count of gateway tool calls that returned ok
    tool_iterations       INTEGER, -- tool-loop iterations used
    tool_loop_reason      TEXT,    -- completed | iteration_cap | loop_timeout | no_successful_tool_exec | backend_error
    -- langfuse-source (2026-06-26): caller attribution tag (X-Bifrost-Source header)
    source                TEXT,    -- cc_dispatch | session_o | (empty: untagged/legacy row)
    -- routing-signal (2026-08-27): the max_tokens the CLIENT declared. METRIC ONLY -- it is
    -- deliberately never sent to a local backend (see backends/ollama.py). NULL means the
    -- caller declared nothing, which is NOT the same as declaring 0.
    max_tokens            INTEGER,
    -- autopilot-join (2026-09-07 DB audit): the identity an autopilot leg carries.
    -- This table had NO key to any other store, so a routing decision could not be
    -- joined to the verification outcome it produced -- only timestamp-correlated.
    -- Empty on INTERACTIVE rows, which have no graph. The 3,111 pre-existing
    -- AUTOPILOT rows stay NULL: there is no key to backfill FROM.
    graph_id              TEXT,
    subtask_id            TEXT,
    -- verification verdict (2026-09-07). NOT written at INSERT -- the verdict does not
    -- exist yet when the row is created. verify_subtask_node fills it in afterwards via
    -- update_verify_status_sync. NULL means: never verified, verification never reached
    -- a terminal exit, or the row predates this column. NULL is not a pass.
    verify_status         TEXT,
    verify_level          INTEGER,
    -- tier-declaration record (2026-09-11). What the CALLER declared, as opposed to what the
    -- classifier picked: X-Tier-Override (source 'header') or a model-alias pin ('alias_pin').
    -- NULL on undeclared rows. honored = 1 only when this row's answer came from the declared
    -- tier. outcome: honored | rerouted | unknown_ignored | refused_retired |
    -- refused_air_gapped | refused_no_tool_tier | refused_ctx_overflow | failed. Refused and
    -- failed rows carry tier_used '' and status REFUSED / ERROR. Appended LAST so a fresh DB
    -- and an ALTER-upgraded DB keep the same column order.
    tier_declared          TEXT,
    tier_declared_source   TEXT,
    tier_declared_honoured INTEGER,
    tier_declared_outcome  TEXT,
    -- fan-out identity (X-Fanout-Run-Id / X-Fanout-Leg), '' when absent. Deliberately NOT
    -- graph_id/subtask_id: those are the AUTOPILOT join key, and update_verify_status_sync
    -- stamps rows by their value.
    fanout_run_id          TEXT,
    fanout_leg             TEXT
)
"""

# v127 telemetry-wire-in: separate table for embed-call telemetry.
# Embeds don't map 1:1 to inference requests (a single retrieval may issue many
# embed calls; some embeds are off-request like routing_cache_shadow). So they
# get their own table rather than inference_events columns.
_CREATE_EMBED_SQL = """
CREATE TABLE IF NOT EXISTS embed_events (
    embed_id    TEXT PRIMARY KEY,
    timestamp   REAL,
    machine     TEXT,
    model       TEXT,     -- e.g. nomic-embed-text
    endpoint    TEXT,     -- e.g. http://127.0.0.1:11436
    caller      TEXT,     -- routing_cache_shadow | bifrost_v1_embeddings | <other>
    input_count INTEGER,  -- count of texts embedded in the call (usually 1)
    dim         INTEGER,  -- vector dim (768 for nomic-embed-text)
    latency_ms  REAL,
    status      TEXT      -- OK | ERROR
)
"""


class InferenceEvent(BaseModel):
    event_id:             str   = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:            float = Field(default_factory=time.time)
    machine:              str   = MACHINE
    hardware:             str   = HARDWARE
    strategy:             str   = "INTERACTIVE"
    complexity_band:      str   = ""
    tier_used:            str   = ""
    tier_history:         list  = Field(default_factory=list)
    escalation_count:     int   = 0
    model:                str   = ""
    tokens_input:         int   = 0
    tokens_output:        int   = 0
    tokens_total:         int   = 0
    latency_ms:           float = 0.0
    cloud_cost_usd:       float = 0.0
    status:               str   = "PASS"  # PASS | FAILED_NEEDS_HUMAN | ERROR
    # v127 telemetry-wire-in: optional classifier-decision signal. Defaults
    # preserve old behavior (pre-v127 emit sites still produce empty strings /
    # zeros; new sites populate from ClassificationResult + routing telemetry).
    classifier_source:     str   = ""
    classifier_confidence: float = 0.0
    classifier_scores:     str   = ""
    classifier_reasoning:  str   = ""
    hint_override:         int   = 0
    # igor-metrics (2026-06-19): Igor tool-loop invocation telemetry (from
    # response._bifrost_tool_loop). Defaults keep non-tool-loop emit sites unchanged.
    tool_calls_made:       str   = ""
    tool_successful_calls: int   = 0
    tool_iterations:       int   = 0
    tool_loop_reason:      str   = ""
    # routing-signal (2026-08-27): client-declared max_tokens, METRIC ONLY. None (not 0)
    # means the caller declared nothing -- 0 would be indistinguishable from a real 0.
    max_tokens:            int | None = None
    # langfuse-source (2026-06-26): caller-supplied attribution tag from the
    # X-Bifrost-Source request header (cc_dispatch | session_o | <empty: untagged>).
    # Replaces the working-hours heuristic in token_offload_report.py with exact
    # source attribution; also emitted to Langfuse generation metadata.
    source:                str   = ""
    # autopilot-join (2026-09-07): populated only by the AUTOPILOT emit sites in
    # subtask_graph. Empty elsewhere -- an interactive request has no graph or subtask.
    graph_id:              str   = ""
    subtask_id:            str   = ""
    # tier-declaration record (2026-09-11): see read_tier_declaration. None = nothing declared.
    tier_declared:          str | None = None
    tier_declared_source:   str | None = None
    tier_declared_honoured: int | None = None
    tier_declared_outcome:  str | None = None
    fanout_run_id:          str   = ""
    fanout_leg:             str   = ""


def usage_split(usage: dict | None) -> tuple[int, int]:
    """Normalize a backend usage dict to (tokens_input, tokens_output).

    Every live adapter returns the OpenAI shape:
      backends/ollama.py:551-554     prompt_eval_count / eval_count -> prompt_/completion_tokens
      backends/anthropic.py:107-110  input_tokens / output_tokens   -> prompt_/completion_tokens
      backends/openai_compat.py:240  provider passthrough, already OpenAI shape
    The input_tokens/output_tokens keys are accepted too, so a raw Anthropic-shaped dict
    that reaches here unconverted is still read correctly rather than silently zeroed.

    backends/n8n.py:88 returns {"total_tokens": 0} with no split at all. That tier reports
    (0, 0), which is the truthful answer for it.

    NEVER estimates and never raises. A word count is not a token count: writing one into a
    column named tokens_output is exactly how tokens_total came to mean two different things
    at four different emit sites (CEREMONY-89 s2.4). If the backend did not say, this
    returns 0 and the row records that it did not say.
    """
    if not isinstance(usage, dict):
        return 0, 0

    def _pick(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, int) and v > 0:
                return v
        return 0

    return _pick("prompt_tokens", "input_tokens"), _pick("completion_tokens", "output_tokens")


class EmbedEvent(BaseModel):
    """v127 telemetry-wire-in: one row per embed-API call."""
    embed_id:    str   = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:   float = Field(default_factory=time.time)
    machine:     str   = MACHINE
    model:       str   = ""
    endpoint:    str   = ""
    caller:      str   = ""
    input_count: int   = 0
    dim:         int   = 0
    latency_ms:  float = 0.0
    status:      str   = "OK"  # OK | ERROR


def _ensure_inference_events_columns(conn: sqlite3.Connection) -> None:
    """v127 telemetry-wire-in: idempotent ALTER for the 5 classifier-signal columns.
    Mirrors the existing _ensure_routing_table ALTER-then-swallow-OperationalError
    pattern so the migration is safe to run on every _ensure_db() call. Existing
    pre-v127 rows get NULL in the new columns -- nothing destructive."""
    for col_decl in (
        "classifier_source TEXT",
        "classifier_confidence REAL",
        "classifier_scores TEXT",
        "classifier_reasoning TEXT",
        "hint_override INTEGER",
        "tool_calls_made TEXT",
        "tool_successful_calls INTEGER",
        "tool_iterations INTEGER",
        "tool_loop_reason TEXT",
        "source TEXT",  # langfuse-source (2026-06-26)
        "max_tokens INTEGER",  # routing-signal (2026-08-27): client-declared budget
        # autopilot-join (2026-09-07): idempotent ALTER on the live 42k-row DB. SQLite
        # ADD COLUMN is metadata-only -- no table rewrite -- and existing rows get NULL,
        # which is honest: they carry no key and cannot be backfilled.
        "graph_id TEXT",
        "subtask_id TEXT",
        "verify_status TEXT",    # verification verdict (2026-09-07), filled in post-hoc
        "verify_level INTEGER",
        # tier-declaration record (2026-09-11): metadata-only ADD COLUMNs, NULL on old rows.
        "tier_declared TEXT",
        "tier_declared_source TEXT",
        "tier_declared_honoured INTEGER",
        "tier_declared_outcome TEXT",
        "fanout_run_id TEXT",
        "fanout_leg TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE inference_events ADD COLUMN {col_decl}")
        except sqlite3.OperationalError:
            pass


# MoA D2 (2026-09-01) -- per-stage pipeline record.
#
# inference_events has no stage_index and no gate_reason column. Adding two columns to a
# live 40k-row table, for fields only one strategy will ever populate, is a migration with
# no upside. Encoding them into an existing text column (gate_reason -> tool_loop_reason)
# would leave a column whose name says WHERE a value came from rather than WHAT it holds.
# A separate additive table costs nothing and makes the stop-threshold histogram one
# GROUP BY: SELECT gate_reason, COUNT(*) FROM pipeline_stages GROUP BY gate_reason.
_CREATE_PIPELINE_STAGES_SQL = """
CREATE TABLE IF NOT EXISTS pipeline_stages (
    stage_event_id   TEXT PRIMARY KEY,
    request_ref      TEXT,
    timestamp        REAL,
    machine          TEXT,
    strategy         TEXT,
    complexity_band  TEXT,
    stage_index      INTEGER,
    stage_count      INTEGER,
    tier             TEXT,
    role             TEXT,
    gate_reason      TEXT,
    escalated        INTEGER,
    latency_ms       REAL,
    ttft_ms          REAL,
    chars_out        INTEGER,
    tokens_input     INTEGER,
    tokens_output    INTEGER
)
"""


class PipelineStageEvent(BaseModel):
    stage_event_id:  str   = Field(default_factory=lambda: str(uuid.uuid4()))
    request_ref:     str   = ""
    timestamp:       float = Field(default_factory=time.time)
    machine:         str   = MACHINE
    strategy:        str   = "PIPELINE"
    complexity_band: str   = ""
    stage_index:     int   = 0
    stage_count:     int   = 0
    tier:            str   = ""
    role:            str   = ""
    gate_reason:     str   = ""
    escalated:       int   = 0
    latency_ms:      float = 0.0
    ttft_ms:         float = 0.0
    chars_out:       int   = 0
    tokens_input:    int   = 0
    tokens_output:   int   = 0


def write_stage_event_sync(event: PipelineStageEvent) -> None:
    """Synchronous write of ONE pipeline stage. Swallows all exceptions -- a telemetry
    failure must never break the call it measures (same rule as write_embed_event_sync).
    """
    try:
        conn = _ensure_db()
        conn.execute(
            """
            INSERT OR IGNORE INTO pipeline_stages (
                stage_event_id, request_ref, timestamp, machine, strategy,
                complexity_band, stage_index, stage_count, tier, role,
                gate_reason, escalated, latency_ms, ttft_ms, chars_out,
                tokens_input, tokens_output
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
            ,
            (
                event.stage_event_id, event.request_ref, event.timestamp,
                event.machine, event.strategy, event.complexity_band,
                event.stage_index, event.stage_count, event.tier, event.role,
                event.gate_reason, event.escalated, event.latency_ms,
                event.ttft_ms, event.chars_out, event.tokens_input,
                event.tokens_output,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] pipeline_stages write failed: %s", exc)


_DB_INITIALIZED = False
# CC amendment (v436 verification): remember WHICH db file got the schema. A new TELEMETRY_DB path, or a db
# file recreated while the process runs, must get the schema again -- a bare process-wide flag skipped it and
# every later write failed with "no such table: inference_events".
_DB_INITIALIZED_PATH = None
_DB_LOCK = threading.Lock()


def _ensure_db() -> sqlite3.Connection:
    global _DB_INITIALIZED, _DB_INITIALIZED_PATH
    _db_key = str(TELEMETRY_DB)
    _fresh = not os.path.exists(_db_key)
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_key, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    if _fresh or not _DB_INITIALIZED or _DB_INITIALIZED_PATH != _db_key:
        with _DB_LOCK:
            if _fresh or not _DB_INITIALIZED or _DB_INITIALIZED_PATH != _db_key:
                conn.execute(_CREATE_SQL)
                _ensure_inference_events_columns(conn)  # v127: upgrade pre-v127 DBs additively
                conn.execute(_CREATE_EMBED_SQL)         # v127: embed-events table (idempotent)
                _ensure_routing_table(conn)             # v98 -- routing_decisions table
                conn.execute(_CREATE_PIPELINE_STAGES_SQL)   # MoA D2 -- per-stage gate_reason record
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inference_events_timestamp ON inference_events(timestamp);")
                conn.commit()
                _DB_INITIALIZED = True
                _DB_INITIALIZED_PATH = _db_key
    return conn


def write_event_sync(event: InferenceEvent) -> None:
    """Synchronous write -- called via thread pool, never blocks the event loop."""
    try:
        conn = _ensure_db()
        # v127: named-column INSERT (positional is brittle when columns get added
        # via ALTER -- the schema's column count may differ between fresh DBs
        # built from _CREATE_SQL and ALTER-upgraded DBs; named is robust).
        conn.execute(
            """
            INSERT OR IGNORE INTO inference_events (
                event_id, timestamp, machine, hardware, strategy, complexity_band,
                tier_used, tier_history, escalation_count, model, tokens_input,
                tokens_output, tokens_total, latency_ms, cloud_cost_usd, status,
                classifier_source, classifier_confidence, classifier_scores,
                classifier_reasoning, hint_override,
                tool_calls_made, tool_successful_calls, tool_iterations, tool_loop_reason,
                source, max_tokens,
                graph_id, subtask_id,
                tier_declared, tier_declared_source, tier_declared_honoured,
                tier_declared_outcome, fanout_run_id, fanout_leg
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.event_id, event.timestamp, event.machine, event.hardware,
                event.strategy, event.complexity_band, event.tier_used,
                json.dumps(event.tier_history), event.escalation_count,
                event.model, event.tokens_input, event.tokens_output,
                event.tokens_total, event.latency_ms, event.cloud_cost_usd,
                event.status,
                event.classifier_source, event.classifier_confidence,
                event.classifier_scores, event.classifier_reasoning,
                event.hint_override,
                event.tool_calls_made, event.tool_successful_calls,
                event.tool_iterations, event.tool_loop_reason,
                event.source, event.max_tokens,
                event.graph_id, event.subtask_id,
                event.tier_declared, event.tier_declared_source,
                event.tier_declared_honoured, event.tier_declared_outcome,
                event.fanout_run_id, event.fanout_leg,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] write failed: %s", exc)


def update_verify_status_sync(graph_id: str, subtask_id: str,
                              status: str, level: int | None = None) -> None:
    """Stamp the verification verdict onto the inference_events row for this attempt.

    `status` on that row is the EXECUTE-phase status -- written before verification and
    hardcoded PASS -- so it cannot answer "did this leg pass". This fills in the column
    that can.

    Addressed by (graph_id, subtask_id), newest row. Verification immediately follows the
    execute that emitted the row; concurrent legs differ by subtask_id; retries of one leg
    are sequential. So the newest row for the pair is the attempt being verified.

    Best-effort, like every other writer here: a telemetry update must never fail a
    verification.
    """
    if not graph_id and not subtask_id:
        return                      # nothing to address the row by
    try:
        conn = _ensure_db()
        conn.execute(
            """
            UPDATE inference_events SET verify_status = ?, verify_level = ?
             WHERE event_id = (SELECT event_id FROM inference_events
                                WHERE graph_id = ? AND subtask_id = ?
                                ORDER BY timestamp DESC LIMIT 1)
            """
            , (status, level, graph_id, subtask_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] verify-status update failed: %s", exc)


def write_embed_event_sync(event: EmbedEvent) -> None:
    """v127 telemetry-wire-in: synchronous embed-event write. Thread-pool
    callable, never blocks event loop, swallows all exceptions (an embed
    telemetry failure must NEVER break a retrieval call)."""
    try:
        conn = _ensure_db()
        conn.execute(
            """
            INSERT OR IGNORE INTO embed_events (
                embed_id, timestamp, machine, model, endpoint, caller,
                input_count, dim, latency_ms, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event.embed_id, event.timestamp, event.machine, event.model,
                event.endpoint, event.caller, event.input_count, event.dim,
                event.latency_ms, event.status,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] write_embed_event_sync failed: %s", exc)


async def write_embed_event(event: EmbedEvent) -> None:
    """v127 async wrapper -- offloads SQLite write to thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_embed_event_sync, event)


async def write_event(event: InferenceEvent) -> None:
    """Async wrapper -- offloads SQLite write to thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_event_sync, event)


# -- Tier-declaration record (2026-09-11) ---------------------------------------------------
# PENDING-router-telemetry-tier-override-record-2026-09-11. Router v434 recorded a request sent
# with X-Tier-Override exactly like a classifier pick (live: hint_override=0,
# classifier_source=routing_cache, tier_history=["1a-hearth"]). hint_override is the
# X-Complexity-Hint flag and never described X-Tier-Override; no column did. The orchestrator
# fan-out legs DECLARE their tier through the Router, so the declaration, whether it was
# honored, and the fan-out run/leg identity have to survive into the row.
#
# read_tier_declaration MIRRORS main.py's X-Tier-Override block (header beats alias pin, a
# retired value is refused, any other unknown value is ignored); it decides nothing about
# routing. It never raises: on any error it returns an empty declaration. Only header VALUES
# matching _DECL_TOKEN_RE are stored; nothing from the request body is read or stored.

DECL_HEADER = "X-Tier-Override"
FANOUT_RUN_HEADER = "X-Fanout-Run-Id"
FANOUT_LEG_HEADER = "X-Fanout-Leg"
DECL_SOURCE_HEADER = "header"
DECL_SOURCE_ALIAS_PIN = "alias_pin"
DECL_IN_FORCE = "declared"
DECL_HONOURED = "honored"
DECL_REROUTED = "rerouted"
DECL_UNKNOWN_IGNORED = "unknown_ignored"
DECL_REFUSED_RETIRED = "refused_retired"
_DECL_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")


def _decl_token(value) -> str:
    """A header value fit to store: 1-64 chars of [A-Za-z0-9._:-], else ''."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if _DECL_TOKEN_RE.fullmatch(value) else ""


class TierDeclaration:
    """What the caller declared for one request. status '' means nothing was declared."""

    __slots__ = ("tier", "source", "status", "fanout_run_id", "fanout_leg")

    def __init__(self, tier=None, source=None, status="", fanout_run_id="", fanout_leg=""):
        self.tier = tier
        self.source = source
        self.status = status
        self.fanout_run_id = fanout_run_id
        self.fanout_leg = fanout_leg

    @property
    def recordable(self) -> bool:
        """True when the request carried a declaration (in force, ignored or refused)."""
        return bool(self.status)

    def fields(self, tier_used=None, outcome=None) -> dict:
        """InferenceEvent keyword fields. tier_used is the tier that ANSWERED; an explicit
        outcome (refusal / failure rows) replaces the derived one."""
        out = {
            "tier_declared": None,
            "tier_declared_source": None,
            "tier_declared_honoured": None,
            "tier_declared_outcome": None,
            "fanout_run_id": self.fanout_run_id,
            "fanout_leg": self.fanout_leg,
        }
        if not self.status:
            return out
        if outcome is None:
            if self.status != DECL_IN_FORCE:
                outcome = self.status
            elif tier_used is not None and tier_used == self.tier:
                outcome = DECL_HONOURED
            else:
                outcome = DECL_REROUTED
        out.update(
            tier_declared=self.tier,
            tier_declared_source=self.source,
            tier_declared_honoured=1 if outcome == DECL_HONOURED else 0,
            tier_declared_outcome=outcome,
        )
        return out


def read_tier_declaration(headers, alias_tier, retired, tier_enum) -> TierDeclaration:
    """Read the declaration off the request headers (see the section comment above)."""
    try:
        run_id = _decl_token(headers.get(FANOUT_RUN_HEADER, ""))
        leg = _decl_token(headers.get(FANOUT_LEG_HEADER, ""))
        raw = (headers.get(DECL_HEADER, "") or "").strip()
        if raw:
            if raw in retired:
                return TierDeclaration(_decl_token(raw) or None, DECL_SOURCE_HEADER,
                                       DECL_REFUSED_RETIRED, run_id, leg)
            try:
                return TierDeclaration(tier_enum(raw).value, DECL_SOURCE_HEADER,
                                       DECL_IN_FORCE, run_id, leg)
            except ValueError:
                if alias_tier is None:
                    return TierDeclaration(_decl_token(raw) or None, DECL_SOURCE_HEADER,
                                           DECL_UNKNOWN_IGNORED, run_id, leg)
        if alias_tier is not None:
            return TierDeclaration(alias_tier.value, DECL_SOURCE_ALIAS_PIN, DECL_IN_FORCE,
                                   run_id, leg)
        return TierDeclaration(None, None, "", run_id, leg)
    except Exception as exc:  # never break the request it describes
        logging.warning("[telemetry] tier declaration unreadable: %s", exc)
        return TierDeclaration()


def declared_failure_event(decl, *, status, outcome, tried=(), band="", source="",
                           max_tokens=None, latency_ms=0.0) -> InferenceEvent:
    """The one row a DECLARED request leaves when it is refused, or fails before any tier
    answers. tier_used is '' (no tier answered; it also keeps free_tier_budget's
    tier_used LIKE '3-%' daily count honest) and every token count is 0."""
    tried = [str(t) for t in (tried or ()) if t]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
        max_tokens = None
    return InferenceEvent(
        strategy="INTERACTIVE",
        complexity_band=band or "",
        tier_used="",
        tier_history=tried,
        escalation_count=max(len(tried) - 1, 0),
        model="",
        latency_ms=float(latency_ms or 0.0),
        status=status,
        source=source or "",
        max_tokens=max_tokens,
        **decl.fields(outcome=outcome),
    )


def schedule_event_write(event, write_fn, task_set=None) -> None:
    """Fire-and-forget write on the running loop, with the retain-set pattern used by
    emit_completion_telemetry_fire_and_forget."""
    task = asyncio.get_running_loop().create_task(write_fn(event))
    if task_set is not None:
        task_set.add(task)
        task.add_done_callback(task_set.discard)


# ── Query helpers (used by FastAPI endpoints in main.py) ────────────────────

def query_recent(n: int = 20) -> list[dict]:
    try:
        conn = _ensure_db()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(inference_events)").fetchall()]
        rows = conn.execute(
            "SELECT * FROM inference_events ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logging.warning("[telemetry] query_recent failed: %s", exc)
        return []


def query_classifier_signal(n: int = 100) -> list[dict]:
    """v127 telemetry-wire-in: read-only query of recent classifier signals.
    Returns rows with the per-request classifier-decision detail (which path,
    its confidence, rule scores, reasoning, hint usage) for follow-up
    misclass / threshold-tuning analysis. Skips pre-v127 rows (classifier_source
    empty)."""
    try:
        conn = _ensure_db()
        rows = conn.execute(
            """
            SELECT event_id, timestamp, complexity_band, classifier_source,
                   classifier_confidence, classifier_scores, classifier_reasoning,
                   hint_override
            FROM inference_events
            WHERE classifier_source IS NOT NULL AND classifier_source != ''
            ORDER BY timestamp DESC LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
        cols = ["event_id", "timestamp", "complexity_band", "classifier_source",
                "classifier_confidence", "classifier_scores",
                "classifier_reasoning", "hint_override"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logging.warning("[telemetry] query_classifier_signal failed: %s", exc)
        return []


def query_embed_recent(n: int = 50) -> list[dict]:
    """v127 telemetry-wire-in: read-only query of recent embed-call telemetry."""
    try:
        conn = _ensure_db()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(embed_events)").fetchall()]
        rows = conn.execute(
            "SELECT * FROM embed_events ORDER BY timestamp DESC LIMIT ?", (n,),
        ).fetchall()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as exc:
        logging.warning("[telemetry] query_embed_recent failed: %s", exc)
        return []


def query_band_distribution() -> dict:
    try:
        conn = _ensure_db()
        rows = conn.execute(
            "SELECT complexity_band, COUNT(*) FROM inference_events GROUP BY complexity_band"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception as exc:
        logging.warning("[telemetry] query_band_distribution failed: %s", exc)
        return {}


def query_cloud_spend() -> dict:
    try:
        conn = _ensure_db()
        total = conn.execute(
            "SELECT SUM(cloud_cost_usd) FROM inference_events"
        ).fetchone()[0] or 0.0
        by_tier = conn.execute(
            "SELECT tier_used, SUM(cloud_cost_usd) FROM inference_events GROUP BY tier_used"
        ).fetchall()
        conn.close()
        return {
            "total_usd": round(total, 6),
            "by_tier":   {r[0]: round(r[1], 6) for r in by_tier},
        }
    except Exception as exc:
        logging.warning("[telemetry] query_cloud_spend failed: %s", exc)
        return {"total_usd": 0.0, "by_tier": {}}


def query_cost_by_period() -> dict:
    """Return cloud spend broken down by day/week/month + running total."""
    import time as _time
    now = _time.time()
    day_start   = now - 86400
    week_start  = now - 86400 * 7
    month_start = now - 86400 * 30
    try:
        conn = _ensure_db()
        def _sum(since: float) -> float:
            r = conn.execute(
                "SELECT SUM(cloud_cost_usd) FROM inference_events WHERE timestamp >= ?",
                (since,)
            ).fetchone()[0]
            return round(r or 0.0, 6)

        result = {
            "today_usd":   _sum(day_start),
            "week_usd":    _sum(week_start),
            "month_usd":   _sum(month_start),
            "total_usd":   _sum(0),
            "cap_day_usd": 20.0,
            "cap_remaining_today_usd": round(max(0.0, 20.0 - _sum(day_start)), 6),
        }
        conn.close()
        return result
    except Exception as exc:
        logging.warning("[telemetry] query_cost_by_period failed: %s", exc)
        return {"today_usd": 0.0, "week_usd": 0.0, "month_usd": 0.0,
                "total_usd": 0.0, "cap_day_usd": 20.0, "cap_remaining_today_usd": 20.0}


def check_daily_cap_exceeded(cap_usd: float = 20.0) -> bool:
    """Returns True if today cloud spend >= cap. Used by fan_out before cloud dispatch."""
    import time as _time
    day_start = _time.time() - 86400
    try:
        conn = _ensure_db()
        r = conn.execute(
            "SELECT SUM(cloud_cost_usd) FROM inference_events WHERE timestamp >= ?",
            (day_start,)
        ).fetchone()[0] or 0.0
        conn.close()
        return float(r) >= cap_usd
    except Exception:
        return False  # fail open -- don't block on telemetry error

# ---------------------------------------------------------------------------
# v98 -- RoutingDecision telemetry (NPU live promotion)
# ---------------------------------------------------------------------------
# Captures one row per routing decision in chat_completions. Linked to
# InferenceEvent via request_id (same UUID generated at handler entry).
# Used to drive threshold tuning + classifier accuracy analysis.
# ---------------------------------------------------------------------------

_CREATE_ROUTING_SQL = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id      TEXT PRIMARY KEY,
    request_id       TEXT,
    timestamp        REAL,
    machine          TEXT,
    tier_chosen      TEXT,
    routing_source   TEXT,
    npu_pred         TEXT,
    npu_conf         REAL,
    npu_latency_ms   REAL,
    rule_pred        TEXT,
    fallback_reason  TEXT,
    agreed           INTEGER,
    model_called     TEXT,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    -- CAREFUL (2026-09-09, telemetry item 5): despite the name this is the BACKEND
    -- CALL ONLY. Its timer starts inside the tier-call loop AFTER routing and
    -- classification have already run (main.py t_start at 2654 / 2707, delivered here
    -- by update_routing_decision_completion), so it EXCLUDES npu_latency_ms.
    -- Measured 2026-09-09 over the 37,182 rows where BOTH latencies are non-null (of
    -- 41,678 total; total_latency_ms is null on 1,596 and npu_latency_ms on 3,674 --
    -- comparing on all rows understates it, so the comparable subset is used here):
    --     AVG(total_latency_ms) = 15,125.3 ms   AVG(npu_latency_ms) = 3,584.1 ms
    --     npu_latency_ms > total_latency_ms on 15,703 of 37,182 rows = 42.2%
    -- That is not corruption -- it is the classifier outrunning the backend call on
    -- short requests, which is where a fixed ~3.6 s routing cost hurts most.
    -- TRUE request latency = total_latency_ms + npu_latency_ms = 18,709.4 ms, of which
    -- routing is 19.2%. NOTHING in this repo computes that sum; analysis_query.py:34 and
    -- analysis_query2.py:28 report both averages side by side without it, so a reader who
    -- divides one by the other gets 23.7% instead of 19.2%.
    -- Do NOT redefine this column in place: 41,678 historical rows
    -- carry the backend-only meaning and a redefinition ends the series.
    -- A separate request_latency_ms column was considered and deliberately NOT added:
    -- its only possible writer is the unused perf_counter anchor at main.py:1793
    -- (request.state.bifrost_request_start, set once and read nowhere), and main.py is
    -- RULE-1 protected. Adding the column now would create another always-NULL column,
    -- which is the F8 defect rather than a fix. Draft in
    -- coordination/done/OUTCOME-telemetry-machine-and-latency-2026-09-09.md.
    -- NB the name is ALREADY TAKEN elsewhere: the Prometheus histogram
    -- bifrost_request_latency_ms (metrics.py:80-85, read by grafana-dashboard.json and
    -- BifrostPortal.jsx) observes RoutingEvent.latency_ms, which is also
    -- post-classification. Same misleading name, different series, same defect.
    total_latency_ms REAL,
    query_features   TEXT,
    -- MEASURED NULL on 41,678 / 41,678 rows (2026-09-09; four query forms plus
    -- typeof() agree). RoutingDecision below has no response_mode field and
    -- write_routing_decision_sync does not insert one. The value exists only as
    -- main.py's local _response_mode (main.py:2311, reassigned 2432) -- computed 141
    -- lines after the constructor that would have carried it, then used only for the
    -- X-Response-Mode header. Left UNWIRED deliberately: a repo-wide grep finds zero
    -- readers of this column, so wiring it would add a write with no consumer, and
    -- the value's source is main.py, which is RULE-1 protected. Populate-or-drop
    -- remains open -- see the 2026-09-09 outcome doc.
    response_mode    TEXT
)
"""


def _ensure_routing_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_ROUTING_SQL)
    # v99 -- defensive ALTER for DBs created under v98 schema (no tokens_out / total_latency_ms).
    # v104 -- query_features / response_mode added.
    # Idempotent: each ALTER raises sqlite3.OperationalError if column exists, swallow it.
    for col_decl in (
        "tokens_out INTEGER",
        "total_latency_ms REAL",
        "query_features TEXT",
        "response_mode TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE routing_decisions ADD COLUMN {col_decl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


class RoutingDecision(BaseModel):
    decision_id:      str   = Field(default_factory=lambda: str(uuid.uuid4()))
    request_id:       str   = ""
    timestamp:        float = Field(default_factory=time.time)
    machine:          str   = MACHINE
    tier_chosen:      str   = ""
    routing_source:   str   = ""                            # npu | rule | fallback | autopilot (v100)
    npu_pred:         str | None = None
    npu_conf:         float | None = None
    npu_latency_ms:   float | None = None
    rule_pred:        str | None = None
    fallback_reason:  str | None = None                     # low_confidence | npu_timeout | npu_error
    agreed:           bool | None = None
    model_called:     str = ""
    tokens_in:        int = 0
    tokens_out:       int | None = None       # v99 -- populated at completion
    total_latency_ms: float | None = None     # v99 -- populated at completion


def write_routing_decision_sync(rd: RoutingDecision) -> None:
    """Synchronous routing-decision write -- thread-pool callable, never blocks event loop."""
    try:
        conn = _ensure_db()  # routing_decisions table created in _ensure_db now
        agreed_int = 1 if rd.agreed is True else (0 if rd.agreed is False else None)
        conn.execute(
            "INSERT OR IGNORE INTO routing_decisions ("
            "decision_id, request_id, timestamp, machine, tier_chosen, "
            "routing_source, npu_pred, npu_conf, npu_latency_ms, rule_pred, "
            "fallback_reason, agreed, model_called, tokens_in"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rd.decision_id, rd.request_id, rd.timestamp, rd.machine,
                rd.tier_chosen, rd.routing_source, rd.npu_pred, rd.npu_conf,
                rd.npu_latency_ms, rd.rule_pred, rd.fallback_reason,
                agreed_int, rd.model_called, rd.tokens_in,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] write_routing_decision_sync failed: %s", exc)


async def write_routing_decision(rd: RoutingDecision) -> None:
    """Async wrapper -- offloads SQLite write to thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_routing_decision_sync, rd)

# ---------------------------------------------------------------------------
# v99 -- completion-side update for routing_decisions
# ---------------------------------------------------------------------------
# After a request completes (chat_completions handler reaches the response
# JSON build), we update the row keyed by request_id with tokens_out and
# total_latency_ms. Streaming responses are not yet hooked (deferred to v100);
# their rows keep tokens_out / total_latency_ms NULL.
# ---------------------------------------------------------------------------

def update_routing_decision_completion_sync(
    request_id: str,
    tokens_out: int,
    total_latency_ms: float,
) -> None:
    """Synchronous UPDATE on existing routing_decisions row. Thread-pool callable."""
    if not request_id:
        return
    try:
        conn = _ensure_db()  # _ensure_routing_table already runs in _ensure_db; ALTERs idempotent
        conn.execute(
            "UPDATE routing_decisions SET tokens_out = ?, total_latency_ms = ? "
            "WHERE request_id = ?",
            (int(tokens_out) if tokens_out is not None else None,
             float(total_latency_ms) if total_latency_ms is not None else None,
             request_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logging.warning("[telemetry] update_routing_decision_completion_sync failed: %s", exc)


async def update_routing_decision_completion(
    request_id: str,
    tokens_out: int,
    total_latency_ms: float,
) -> None:
    """Async wrapper -- offloads UPDATE to thread pool, never blocks event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        update_routing_decision_completion_sync,
        request_id,
        tokens_out,
        total_latency_ms,
    )

# ---------------------------------------------------------------------------
# v100 -- shared completion-hook helper (used from streaming, non-streaming,
# and AUTOPILOT exit paths). Single entry point; both InferenceEvent and
# routing_decisions UPDATE are handled here.
# ---------------------------------------------------------------------------

async def emit_completion_telemetry(
    request_id: str,
    tokens_out: int,
    total_latency_ms: float,
    *,
    inference_event: "InferenceEvent | None" = None,
) -> None:
    """
    Single completion telemetry entry point used from all completion paths
    (streaming, non-streaming, AUTOPILOT). Updates routing_decisions row
    keyed on request_id; optionally writes InferenceEvent.

    Never raises -- all underlying writers swallow exceptions and log warnings.
    """
    await update_routing_decision_completion(
        request_id=request_id,
        tokens_out=tokens_out,
        total_latency_ms=total_latency_ms,
    )
    if inference_event is not None:
        await write_event(inference_event)
        # langfuse-source (2026-06-26): best-effort, env-gated Langfuse generation.
        # No-op when keys absent; never raises (see langfuse_client.emit_generation).
        try:
            import langfuse_client
            await langfuse_client.emit_generation(inference_event)
        except Exception as _lf_exc:  # pragma: no cover -- tracing must never break telemetry
            logging.debug("[telemetry] langfuse emit skipped: %s", _lf_exc)


def emit_completion_telemetry_fire_and_forget(
    request_id: str,
    tokens_out: int,
    total_latency_ms: float,
    inference_event: "InferenceEvent | None" = None,
    task_set: set | None = None,
) -> None:
    """
    Fire-and-forget wrapper for emit_completion_telemetry.

    Schedules on the running event loop with the v98/v99 retain-set GC pattern
    (rule #29 from v98 lessons). Caller passes the module-level _telemetry_tasks
    set to keep the task object alive until done_callback discards it.

    If task_set is None, the task is bare-create_task (technically GC-eligible
    -- callers should always pass task_set unless they don't care about completion).
    """
    _t = asyncio.create_task(emit_completion_telemetry(
        request_id=request_id,
        tokens_out=tokens_out,
        total_latency_ms=total_latency_ms,
        inference_event=inference_event,
    ))
    if task_set is not None:
        task_set.add(_t)
        _t.add_done_callback(task_set.discard)

