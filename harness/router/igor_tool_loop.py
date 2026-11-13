"""
igor_tool_loop.py -- Server-side tool-execution loop for Igor (T2.5 + tool-capable peers).

MCP-3A-BUILD v119 (architect-signed 2026-05-21). See dispatch-journal
2026-05-21-mcp-3a-phase1-design.md for the design artifact.

This module orchestrates a bounded model<->tool iteration cycle:
  1. Caller (main.py) invokes run_tool_loop with a backend dispatch fn
     and a gateway-call fn (closures so the module stays decoupled
     from main.py + httpx specifics; testable in isolation).
  2. Loop sends the messages array (post IGOR_BASELINE inject) to the
     backend with the safe-subset tool definitions and tool_choice=auto.
  3. If the model emits tool_calls, each is filtered against SAFE_TOOL_NAMES
     (allowlist primary gate), then dispatched through the gateway. The
     gateway's @gated_tool chokepoint (bifrost_shell.py) is the policy
     enforcer -- this loop is an orchestrator, not a second gate.
  4. Results are appended to messages as role:"tool" entries with the
     matching tool_call_id, then the loop iterates.
  5. Terminates when no tool_calls in a response, max_iterations is hit,
     total_turn_timeout_s elapses, a backend dispatch raises, or the prompt no
     longer fits the tier's context window after compaction (Ceremony 4 P1).
  6. Never executes tool calls from a reply the backend cut off at its output
     token limit (finish_reason "length"): each gets an error result asking the
     model to re-issue it, and the loop continues (Ceremony 4 P4).
  7. Pages a tool result that is over the cap instead of losing the rest
     (tool_output_page, in-process); at the iteration cap asks the model once,
     without tools, for a handoff; refuses a call identical to the previous two
     (Ceremony 4 P3).

Safety invariants:
  - Loop NEVER raises on normal/expected paths. Always returns an OpenAI-shape
    chat completion dict. (One surgical exception: ToolGateBreach is raised
    fail-closed if a denied/unknown tool ever EXECUTES past the allowlist gate --
    unreachable unless the gate has a hole; Ceremony 1, 2026-06-27. Pattern 2,
    2026-09-11: a breach inside a delegated leaf now halts the parent turn too.
    Opt-in second exception: with raise_on_backend_error=True a backend failure
    that survives the loop's recovery raises ToolLoopBackendError.)
  - SAFE_TOOL_NAMES is POLICY-DERIVED (Ceremony 3) and filters tool_calls BEFORE
    the gateway call -- it is exactly the policy enabled-class tools (READ/ANALYZE/
    DELEGATE). The denied-class tools write_file / run_python / autopilot_submit are
    excluded by construction; an unclassified gateway tool is excluded fail-closed.
  - tool_calls[].function.arguments may arrive as a dict (router Ollama-native
    shape) or as a JSON string (direct OpenAI shape). Normalized to dict
    before the gateway call.
  - Shape-only logging (rule 42): tool name + arg keys + arg size only;
    never the arg VALUES, never the tool result content (which can carry
    secrets). Tool results live in process memory (messages list) only.
  - Iteration cap is the load-bearing doom-loop break; not configurable by
    the model.

This module is invoked from main.py only when the capability gate
_should_use_tool_loop in main.py returns True (model in allowlist + no
inbound tools[] + gateway URL set + fleet_config tool_use consumer agrees).
"""

import asyncio
import datetime as _dt
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

import policy_instructions
from context_overflow import estimate_prompt_tokens as _estimate_tokens  # Ceremony 4 P1
import hashlib  # Ceremony 4 P3: doom-loop call hash

# Ceremony 6 (2026-09-21). The halt switch, imported the same guarded way
# bifrost_shell.py imports it, so the two copies of this decision read alike: a
# missing module degrades to "not halted" and can never take the tool loop down.
#
# DEPLOYMENT NOTE, recorded here because it is the whole point of the check: the
# router image built before 2026-09-21 does NOT contain bifrost_halt.py (verified
# in the live pod). Until the router is rebuilt, _bifrost_halt is None here and
# the check below is INERT. That is a deployment gap, and writing it down is what
# keeps it from being a silent one -- see OUTCOME-ceremony6.
try:
    import bifrost_halt as _bifrost_halt
except Exception:                                   # noqa: BLE001
    _bifrost_halt = None

log = logging.getLogger("bifrost.tool_loop")

# ---------------------------------------------------------------------------
# Ceremony 3 (igor-dynamic-tools-ceremony3, 2026-06-27): SAFE_TOOL_NAMES is no
# longer a hand-authored frozenset -- it is POLICY-DERIVED below (after the policy
# load), once per module load. A tool is advertised/routable for a role iff it is in
# policy_instructions.allowed_tools(role) -- its class is enabled in
# bifrost_shell_policy.json AND granted to the role in
# policy_instructions.ROLE_ENABLED_CLASSES -- and it is not in ADVERTISED_EXCLUDE. The class default is
# UNKNOWN (fail-closed): an unclassified gateway tool is DENIED advertising until it
# is classified in bifrost_shell_policy.json -- so dynamic reflection can never
# silently widen the surface. WRITE/EXECUTE classes are disabled, so write_file /
# run_python / autopilot_submit are excluded by construction. bifrost_shell.py
# @gated_tool stays the enforcement chokepoint; this gate is the orchestrator's
# policy-derived mirror (defense in depth).
#
# 2026-09-11 (gate-docs drift): a module constant ENABLED_CLASSES = {READ, ANALYZE,
# DELEGATE} stood here. No code read it, and it disagreed with the live gate: igor and
# leaf are also granted SANDBOX (run_in_sandbox) and READ_FS (the host-FS readers). It
# was removed so no reader takes it for the gate. Tool COUNTS are not written in these
# comments either -- every copy went stale on the next grant; compute
# len(policy_instructions.allowed_tools(role)). tests/test_tool_gate_docs_drift.py
# fails if either comes back.
# ---------------------------------------------------------------------------
# Option i (John, 2026-06-27): empty exclude -- the advertised set is PURELY the
# policy enabled-class tools, with no hand-maintained curation layer left to drift.
# (A re-curation hook: add tool names here to suppress advertising without denying
# the class.)
ADVERTISED_EXCLUDE = frozenset()


def _repo_path_schema() -> dict:
    """Schema for a `repo_path` tool argument.

    NO DEFAULT SHIPS. This used to advertise an absolute path from the machine the
    file was written on -- and it advertised it to the MODEL, as the value to use when
    the caller omits one. It was wrong anywhere else and it carried an account name.
    The default now comes from BIFROST_REPO_PATH and is simply ABSENT when that is
    unset, so the model is asked for a path rather than handed one that does not exist.

    Built per call rather than at import, so setting the variable does not require a
    restart to take effect and a test can set it without reloading the module.
    """
    schema = {"type": "string", "description": "Absolute path to the git repo."}
    configured = os.environ.get("BIFROST_REPO_PATH", "").strip()
    if configured:
        schema["default"] = configured
    return schema

# B2 Option B fence (verify oversight, 2026-06-27): recursive delegation is ALLOWED but
# BOUNDED + OBSERVED. depth 0 = top caller (CC/Igor); depth N = a leaf N levels deep.
# delegate_subtask is REFUSED fail-closed when depth >= MAX_DELEGATE_DEPTH; more than
# DELEGATE_WIDTH_CAP delegate calls in one turn -> the surplus is refused. Every delegate
# attempt is telemetered with role+depth so CC holds + verifies the delegation tree (the
# leaf is non-authoritative). Igor delegates at depth 0, so with MAX>=1 its single-level
# delegation is UNCHANGED -- the fence is inert for Igor. (B2: the dispatch is still a plain
# completion; the bound + telemetry ship now, B3 makes the permitted sub-call a run_tool_loop
# with depth+1, so recursion can never go live unfenced.)
MAX_DELEGATE_DEPTH = 2   # one recursive level: Igor(0)->leaf(1)->leaf(2); a leaf at depth 2 refused
# 2026-09-25 ceremony (John: "APPROVED on the cap ceremony, apply both diffs"). 4 -> 2.
# The phase-2 gather in run_tool_loop awaits every dispatch of a turn CONCURRENTLY, so this is a
# concurrency cap and not merely a per-turn tally. Four of the seven SAFE_LEAVES are Forge-hosted,
# so a cap of 4 admitted Igor(65GB) + gemma4(17GB) + toolmaster(20GB) = 102GB against Forge's
# 100GB GTT -- the combination bifrost_delegate.py L53 forbids in capitals. 2 is the number that
# file's own L54 states ("Max 2 concurrent Forge leaves"). The old 4 was justified only as
# "matches the validated 4-leaf fan-out", which validated WIDTH, never Forge co-residency.
# 2 alone does NOT satisfy John's 2026-09-17 RAM-bus ruling (Igor and T2 must not overlap even
# when the VRAM fits) -- that is what _FORGE_CONCURRENCY_CAP below enforces.
DELEGATE_WIDTH_CAP = 2   # max sub-delegations per turn; the gather makes it a concurrency cap

# Forge-hosted SAFE_LEAVES, named EXPLICITLY. Deliberately NOT parsed out of
# bifrost_delegate.SAFE_LEAVES: the host there is the first column of a free-prose curator note,
# and a guard that depends on prose formatting fails silently the day the note is reworded.
# If a Forge-hosted leaf is added to SAFE_LEAVES it MUST be added here or it bypasses the cap.
_FORGE_LEAVES = frozenset({
    "bifrost-coder-toolmaster",
    "bifrost-toolmaster",
    "bifrost-t2-gemma4",
    "bifrost-igor-solo",
})
_FORGE_CONCURRENCY_CAP = 1   # John 2026-09-17: Igor and T2 share one RAM bus -- sequential.


# ---------------------------------------------------------------------------
# Ceremony 1 (2026-06-27, igor-delegate-route): violation telemetry + the
# policy->instruction drift guard. bifrost_shell_policy.json is the ONE source;
# policy_instructions derives the denied set + the drift assertion from it.
# role is constant ("igor") this ceremony -- the single tool-loop caller; the
# real per-caller role param lands in Path B (Architect 2026-06-27).
# ---------------------------------------------------------------------------
# B2 (2026-06-27): _LOOP_ROLE retired -> run_tool_loop takes a per-caller `role`
# param (Igor is the first caller, the B0/B1 leaf the second). This module-level
# default reproduces the pre-B2 Igor-only sets for the module globals + as the
# run_tool_loop default, so Igor's behavior is byte-unchanged.
_DEFAULT_ROLE = "igor"

# In-pod writer: run_tool_loop runs in the Linux Router pod (k3d on Hearth),
# which CANNOT write a Windows UNC. The bifrost-logs volume (hostPath
# /net-ssd/bifrost-logs) is mounted at /app/logs and surfaces to Windows via the
# decomp_shadow /net-ssd node-bind. Env-overridable.
_VIOLATION_LOG = os.environ.get(
    "BIFROST_TOOL_VIOLATION_LOG", "/app/logs/telemetry/tool-violations.jsonl"
)
# Phase 1b (fleet-invariant-canary framework): the breach event ALSO lands in the
# SHARED canary-events sink so every fleet invariant shares ONE audit trail. Same
# proven node-bind as the violation log; set BIFROST_CANARY_EVENTS_LOG to the
# framework runner's exact sink path. Best-effort -- NEVER affects the raise+halt.
_CANARY_EVENTS_LOG = os.environ.get(
    "BIFROST_CANARY_EVENTS_LOG", "/app/logs/telemetry/canary-events.jsonl")

# Denied set + executable-allowed set, derived from the ONE policy source (never
# hand-authored). Best-effort load: a missing/unreadable policy file degrades to
# empty-denied with a LOUD warning rather than bricking router import; a real
# denied-tool LEAK still raises in the drift guard below.
try:
    _policy = policy_instructions.load_policy()
    _TOOL_CLASS_MAP = dict(_policy.get("tools", {}))
    _DENIED_TOOLS = policy_instructions.denied_tools(_DEFAULT_ROLE, _policy)
    # A tool that EXECUTED must be policy-allowed (enabled class). C3: STACKB_KNOWN
    # retired. B2: these module globals are the IGOR sets (_DEFAULT_ROLE); the leaf's
    # sets are derived per-call in run_tool_loop from the role param.
    _EXECUTABLE_ALLOWED = policy_instructions.allowed_tools(_DEFAULT_ROLE, _policy)
except Exception as _e:  # noqa: BLE001 -- policy unreadable in this env
    # Breach-level visibility (Architect 2026-06-27): a degraded policy load means
    # the denied-set is EMPTY and the breach canary is blind -- a security-relevant
    # degradation, surfaced at CRITICAL like a breach (pod log + morning audit).
    # C3 fail-closed: with no policy, NOTHING is executable-allowed and NOTHING is
    # advertised (SAFE_TOOL_NAMES derives to empty below).
    _policy = None
    log.critical(
        "BIFROST-POLICY-DEGRADED policy load failed; denied-set EMPTY + drift guard "
        f"degraded (breach canary blind) err={type(_e).__name__}"
    )
    _TOOL_CLASS_MAP = {}
    _DENIED_TOOLS = frozenset()
    _EXECUTABLE_ALLOWED = set()


def _derive_safe_tool_names(policy: Optional[dict], role: str = "igor") -> frozenset:
    """C3/B2 (2026-06-27): derive the advertised/routable gate FROM THE POLICY, per ROLE.

    A tool is in the gate iff its policy class is ENABLED for this role and it is not
    in ADVERTISED_EXCLUDE. policy_instructions.allowed_tools(role) is role-scoped (B2):
    igor and leaf -> the classes granted in policy_instructions.ROLE_ENABLED_CLASSES
    (not restated here) -- Option B (John 2026-06-27): the leaf role MAY delegate
    (depth-bounded by MAX_DELEGATE_DEPTH). An UNKNOWN role fail-closes to the default
    READ/ANALYZE (no delegate_subtask, no run_in_sandbox, no host-FS readers).
    A tool with no policy class (tool_class -> 'UNKNOWN') is NOT enabled-class and is
    EXCLUDED -- the FAIL-CLOSED property. A blind policy (load failed) advertises NOTHING."""
    if policy is None:
        return frozenset()
    allowed = policy_instructions.allowed_tools(role, policy)
    return frozenset(t for t in allowed if t not in ADVERTISED_EXCLUDE)


# The policy-derived gate (replaces the v119 hand-authored frozenset): exactly
# policy_instructions.allowed_tools("igor") minus ADVERTISED_EXCLUDE. It includes
# tool_output_page (READ), which the loop serves in-process and advertises only after
# it pages a result, so the advertised list (get_safe_tool_definitions) is this gate
# minus that one tool. WRITE/EXECUTE tools are excluded by construction.
SAFE_TOOL_NAMES = _derive_safe_tool_names(_policy, _DEFAULT_ROLE)

# Startup drift guard (LOUD). C3: the gate is policy-derived, so this CONFIRMS the
# structural invariants. HARD (raise): no denied tool reachable; every gate tool is
# policy-allowed. SOFT (warn): the unclassified set, now EMPTY by construction --
# a non-empty warn here means C2 classification is incomplete or the gate is not
# policy-derived (the C2+C3 acceptance signal). A PolicyDriftError fails import loud.
try:
    _drift = policy_instructions.assert_no_drift(SAFE_TOOL_NAMES, _DEFAULT_ROLE, _policy)
    if _drift.get("unclassified"):
        log.warning(
            "DRIFT: unclassified gate tools should be EMPTY post-C2/C3 but are not: "
            + ", ".join(sorted(_drift["unclassified"]))
        )
except policy_instructions.PolicyDriftError:
    raise  # fail loud: denied tool reachable or rogue (non-policy-allowed) gate entry
except Exception as _e:  # noqa: BLE001 -- policy unreadable; already surfaced above
    log.critical(
        f"BIFROST-POLICY-DEGRADED drift guard skipped (policy unreadable) err={type(_e).__name__}"
    )


class ToolGateBreach(RuntimeError):
    """A DENIED/unknown tool EXECUTED past the allowlist gate -- fleet invariant
    violation. INTENTIONAL, surgical exception to this module's 'never raises'
    contract: a breach is unreachable unless the gate has a hole, and
    fail-closed-loud is required (Ceremony 1, 1B(d)). All normal/expected paths
    still never raise."""


def _violation_record(caller: Any, tool: str, decision: str, args: Any, role: str = "igor",
                      depth: Optional[int] = None, mode: Optional[str] = None) -> dict:
    """Build a SHAPE-ONLY violation record (rule 42: arg types+lengths, never values;
    never the tool result). B2: `role` is the real caller role (igor|leaf); `depth` is the
    delegation depth for delegate fence records (None for ordinary tool records)."""
    shape: dict = {}
    if isinstance(args, dict):
        for k, v in args.items():
            try:
                ln = len(v) if hasattr(v, "__len__") else len(str(v))
            except Exception:  # noqa: BLE001
                ln = -1
            shape[str(k)] = f"{type(v).__name__}:{ln}"
    return {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "caller": str(caller),
        "role": role,
        "depth": depth,
        "tool": str(tool),
        "class": _TOOL_CLASS_MAP.get(tool, "UNKNOWN"),
        "decision": decision,
        "mode": mode,
        "args_shape": shape,
    }


def _emit_violation(record: dict) -> None:
    """Append one JSONL line; best-effort -- telemetry failure NEVER breaks the
    loop (breach handling halts separately and unconditionally)."""
    try:
        os.makedirs(os.path.dirname(_VIOLATION_LOG), exist_ok=True)
        with open(_VIOLATION_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning(f"violation telemetry write failed err={type(e).__name__}")


def _emit_canary_event(record: dict) -> None:
    """Append one fleet-invariant canary event to the SHARED canary-events sink
    (Phase 1b: the breach canary becomes framework instance #1). Best-effort --
    a telemetry failure NEVER affects breach handling (raise+halt is separate and
    unconditional)."""
    try:
        os.makedirs(os.path.dirname(_CANARY_EVENTS_LOG), exist_ok=True)
        with open(_CANARY_EVENTS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning(f"canary-event telemetry write failed err={type(e).__name__}")


def _ha_notify_breach(tool: str, request_id: str) -> None:
    """Fail-closed alert on a gate breach. log.critical ALWAYS (surfaces in pod
    logs + morning audit); best-effort HA webhook if HA_WEBHOOK_URL is
    provisioned in-pod (the pod cannot run the host ha-notify.ps1, so a
    provisioned webhook is the in-pod push channel)."""
    log.critical(
        f"BIFROST-BREACH gate hole: tool='{tool}' executed past allowlist request_id={request_id}"
    )
    url = os.environ.get("HA_WEBHOOK_URL")
    if not url:
        return
    try:
        httpx.post(
            url,
            json={"event": "bifrost_tool_gate_breach", "tool": str(tool), "request_id": str(request_id)},
            timeout=5.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"ha-notify webhook failed err={type(e).__name__}")


# C3 (2026-06-27): the candidate tool universe -- the hardcoded schema pool. Until a
# live Gateway /tools/list exists, this list IS list_tools(). get_safe_tool_definitions()
# advertises only the policy-derived SAFE_TOOL_NAMES subset (fail-closed: a pool tool
# with no enabled policy class is filtered OUT). To advertise a new tool: add its schema
# here AND classify it in bifrost_shell_policy.json.
_ALL_TOOL_DEFINITIONS: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "run_ruff",
                "description": "Lint Python code with ruff and return any style violations.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source code to lint."},
                        "filename": {"type": "string", "description": "Virtual filename for error messages.", "default": "generated.py"},
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_mypy",
                "description": "Type-check Python code with mypy and return any type errors.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source code to type-check."},
                        "filename": {"type": "string", "description": "Virtual filename for error messages.", "default": "generated.py"},
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_checks",
                "description": "Run both ruff and mypy on Python code. Returns combined pass/fail with line-level details.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source code to validate."},
                        "filename": {"type": "string", "description": "Virtual filename for error messages.", "default": "generated.py"},
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "kb_search",
                "description": "Search the BIFROST knowledge base (RAG). Returns top-N matching chunks with source attribution.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "n_results": {"type": "integer", "description": "Number of results to return.", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "router_status",
                "description": "Get current BIFROST Router status including active tier assignments and fleet config.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep_repo",
                "description": "Read-only regex grep over an allow-listed repo (scoped, secret-denied, capped). Returns file:line matches.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regular expression to search for."},
                        "repo": {"type": "string", "enum": ["bifrost-router", "autoagent"], "description": "Which repo to search.", "default": "bifrost-router"},
                        "subpath": {"type": "string", "description": "Optional path within the repo to scope the search.", "default": ""},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "Read-only directory listing of an allow-listed repo (scoped, secret entries elided, capped).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "enum": ["bifrost-router", "autoagent"], "description": "Which repo.", "default": "bifrost-router"},
                        "subpath": {"type": "string", "description": "Optional path within the repo.", "default": ""},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_read",
                "description": "Read-only git inspection of an allow-listed repo. mode is log, show, or diff ONLY (never mutating). Output is secret-redacted + capped.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "enum": ["bifrost-router", "autoagent"], "description": "Which repo.", "default": "bifrost-router"},
                        "mode": {"type": "string", "enum": ["log", "show", "diff"], "description": "git subcommand (read-only).", "default": "log"},
                        "args": {"type": "string", "description": "Optional extra args (refs/paths + read flags only; sanitized).", "default": ""},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "router_health",
                "description": "Get BIFROST Router health: classifier and embedding-backend up/down status.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Read-only `git status` of an allow-listed repo (defaults to bifrost-platform).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "repo_path": _repo_path_schema(),
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "Read-only `git log` of an allow-listed repo: the most recent n commits (defaults to bifrost-platform).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "Number of recent commits to show.", "default": 10},
                        "repo_path": _repo_path_schema(),
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "autopilot_status",
                "description": "Get the status of a BIFROST autopilot graph run by its graph_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "graph_id": {"type": "string", "description": "The autopilot graph run id to query."},
                    },
                    "required": ["graph_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the contents of a file by path (host-scoped at the gateway). Read-only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path of the file to read."},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_subtask",
                "description": "Delegate a subtask to a named BIFROST leaf executor. mode=reasoning (default): the leaf returns a plain completion (no tools). mode=tools: the leaf runs a real bounded tool loop (depth-bounded, opt-in). Returns the leaf's answer, or a structured error on a bad alias / timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "leaf_alias": {"type": "string", "description": "Router alias of the target leaf (e.g. bifrost-1a-hearth, bifrost-coder-toolmaster)."},
                        "subtask": {"type": "string", "description": "The prompt/task to deliver to the leaf."},
                        "timeout": {"type": "integer", "description": "Request timeout in seconds.", "default": 120},
                        "mode": {"type": "string", "enum": ["reasoning", "tools"], "default": "reasoning", "description": "reasoning (default): the leaf returns a plain completion. tools: the leaf runs a real bounded tool loop (depth+1), opt-in."},
                    },
                    "required": ["leaf_alias", "subtask"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_in_sandbox",
                "description": "Execute Python code in the k3d deny-all sandbox namespace (no egress, resource-capped, no host FS, auto-deleted; fail-closed -- the payload runs only after network isolation is confirmed). Returns JSON {ok, reason, stdout}. Use this for untrusted/generated code -- NOT run_python (host-scoped, EXECUTE-denied).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python source code to execute in the sandbox."},
                        "timeout_s": {"type": "integer", "description": "Max seconds for the payload after isolation is confirmed.", "default": 120},
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web and return the top results as "
                               "title/url/snippet. Use for facts that are not in this "
                               "network: upstream release notes, vendor documentation, "
                               "current events. For anything about THIS fleet use "
                               "kb_search, router_status or router_health -- those answer "
                               "from local state and the web cannot know them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Search terms, not a question to an assistant."},
                        "max_results": {"type": "integer",
                                        "description": "How many results to return.",
                                        "default": 3},
                    },
                    "required": ["query"],
                },
            },
        },
]


def _safe_for_role(role: str) -> frozenset:
    """B2: the policy-derived advertised/gate set for a caller role. role=igor returns
    the EXACT module global SAFE_TOOL_NAMES (Igor byte-unchanged); other roles derive
    fresh (leaf: Option B, the same classes as igor, MAY delegate, depth-bounded; an
    UNKNOWN role: READ/ANALYZE only, no delegate_subtask). Fail-closed (blind policy -> empty)."""
    if role == "igor":
        return SAFE_TOOL_NAMES
    return _derive_safe_tool_names(_policy, role)


def _executable_for_role(role: str) -> set:
    """B2: the breach-canary executable-allowed set for a role. role=igor returns the
    EXACT module global _EXECUTABLE_ALLOWED (unchanged). Fail-closed (blind -> empty)."""
    if role == "igor":
        return _EXECUTABLE_ALLOWED
    if _policy is None:
        return set()
    return policy_instructions.allowed_tools(role, _policy)


def get_safe_tool_definitions(role: str = "igor") -> list[dict]:
    """C3/B2 (2026-06-27): advertise ONLY this role's policy-derived subset of the tool
    pool (_ALL_TOOL_DEFINITIONS). role=igor -> SAFE_TOOL_NAMES; leaf -> the same set
    (Option B: leaf MAY delegate); an UNKNOWN role -> READ/ANALYZE only (fail-closed, no
    delegate_subtask). tool_output_page is not in the pool: the loop appends it after the
    first paged result.
    An unclassified or denied-class pool tool is filtered OUT (fail-closed). Schema shape
    unchanged from v119."""
    safe = _safe_for_role(role)
    return [d for d in _ALL_TOOL_DEFINITIONS if d["function"]["name"] in safe]


def _shape_only_log(name: str, arguments: Any, request_id: str) -> None:
    """Rule 42: log argument shape only, never values. Tool results are
    never logged at all -- they live in the messages list (process memory)
    and are returned to the caller."""
    try:
        size = len(json.dumps(arguments))
    except Exception:
        size = -1
    keys = list(arguments.keys()) if isinstance(arguments, dict) else f"non-dict:{type(arguments).__name__}"
    log.info(f"tool_call name={name} args_keys={keys} args_size={size} request_id={request_id}")


GatewayCallFn = Callable[[str, dict, str], Awaitable[dict]]


def make_gateway_caller(
    gateway_url: str,
    gateway_token: str,
    per_tool_timeout_s: float = 30.0,
) -> GatewayCallFn:
    """Build the production gateway caller closure using MCP Streamable HTTP transport.

    Returns a callable `async (name, arguments, request_id) -> dict` where the result dict is:
      {"ok": True,  "result": <tool output>}                              -- success (incl. policy_denied dict from gateway)
      {"ok": False, "kind": "timeout"|"transport"|"tool_error", "error": str}  -- failure
    Never raises.

    v119.1 (2026-05-22 MCP-3A Phase 4 T1 fix): Migrated from POST /tools/call REST
    to MCP Streamable HTTP transport at /mcp. Docker MCP Gateway serves only the
    MCP JSON-RPC protocol at /mcp + SSE; there is no REST tools/call endpoint
    (every other path 307-redirects to /mcp). Uses the mcp SDK that's already in
    requirements.txt (mcp==2.0.0 as of 2026-08-12; was 1.26.0 when this note was written).
    """
    from mcp import ClientSession
    # MCP 2.0.0 (2026-07-28 spec): streamablehttp_client was renamed to
    # streamable_http_client and now takes an http_client instead of headers; auth moves
    # into create_mcp_http_client(headers=...). Verified against the live 8811 gateway.
    from mcp.client.streamable_http import (
        streamable_http_client,
        create_mcp_http_client,
    )

    mcp_url = f"{gateway_url.rstrip('/')}/mcp"
    headers = {"Authorization": f"Bearer {gateway_token}"}

    async def call_gateway(name: str, arguments: dict, request_id: str) -> dict:
        _shape_only_log(name, arguments, request_id)
        try:
            async with asyncio.timeout(per_tool_timeout_s):
                _http = create_mcp_http_client(headers=headers)
                # mcp 2.0.0 yields a 2-TUPLE (read_stream, write_stream) -- confirmed in
                # site-packages/mcp/client/streamable_http.py, which does
                # `yield read_stream, write_stream`. 1.x yielded a 3-tuple (read, write, _).
                # The staged packet specified an object shape with .read/.write attributes and
                # asserted that shape had been executed against 8811; it had not. It raises
                # AttributeError at runtime, on a path neither import nor ast.parse exercises.
                # Caught only by probing the live gateway from inside the pod.
                async with streamable_http_client(mcp_url, http_client=_http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments=arguments)
                        text_parts: list[str] = []
                        for c in result.content:
                            text_parts.append(getattr(c, "text", str(c)))
                        output_text = "\n".join(text_parts) if text_parts else ""
                        if getattr(result, "isError", False):
                            return {"ok": False, "kind": "tool_error", "error": output_text[:500]}
                        try:
                            return {"ok": True, "result": json.loads(output_text)}
                        except (json.JSONDecodeError, ValueError):
                            return {"ok": True, "result": {"text": output_text}}
        except asyncio.TimeoutError:
            return {"ok": False, "kind": "timeout", "error": "gateway request timed out"}
        except Exception as e:
            return {"ok": False, "kind": "transport", "error": f"gateway transport error: {type(e).__name__}: {str(e)[:200]}"}

    return call_gateway


# ---------------------------------------------------------------------------
# Ceremony 4 Pattern 1 (2026-09-11): in-loop context budget.
# The inbound pre-flight (context_overflow.check_context in main.py) runs ONCE, before the
# loop, and without the loop's tool definitions. The loop then re-sent a growing msgs list up
# to 12 times with no check, and on the 8k tiers a single 32 KB tool result (~8k tokens) filled
# the whole window by itself; ollama silently front-truncates the prompt when that happens.
#   1. The per-tool-result cap is derived from the tier's num_ctx (bifrost_delegate git_read's
#      formula), still never above 32 KB.
#   2. Before each backend call after the first, the prompt is estimated. At >= _COMPACT_AT of
#      the budget, tool results OLDER than the latest tool block are cut to head+tail; if that is
#      not enough, the older blocks are summarized ONCE (no tools; the latest block stays
#      verbatim; the summary goes AFTER the task, never in front of it).
#   3. Still over the budget -> terminal reason "context_budget". subtask_graph's give-up branch
#      is a `not in (None, "completed")` test, so it catches the new reason as it is.
# Unknown num_ctx (tests, an unmapped tier) -> none of this runs: today's behavior.
# ---------------------------------------------------------------------------
_RESULT_CAP_MAX = 32 * 1024
_RESULT_CTX_SHARE = 0.20      # one tool result may take this share of the window
_CHARS_PER_TOKEN = 3.5        # same constants as bifrost_delegate git_read's ctx-derived cap
_COMPACT_AT = 0.80            # DSH thresholdRatio; Pi and OpenCode compact near the same point
_PRUNE_HEAD = 4096
_PRUNE_TAIL = 1024
_PRUNE_MARKER = "\n...[older tool result cut to its head and tail to fit the context window]...\n"
_SUMMARY_MAX_TOKENS = 800
_SUMMARY_MAX_CHARS = 4000
# Ceremony 4 P4: the error each tool call gets when its reply hit the output token limit.
_LENGTH_STOP_ERROR = (
    "Your previous reply hit the output token limit, so this call's arguments may be "
    "truncated and it was NOT executed. Re-issue the call, with shorter arguments if you can."
)
_COMPACT_INSTRUCTION = (
    "The conversation is close to this model's context limit. In at most 300 words, summarize "
    "what the tool calls above have established: the facts found, with file names, line numbers "
    "and values exactly as the tools returned them; what is still unknown; and which tool call "
    "should come next. Do not call tools. Do not answer the original request yet."
)
_COMPACT_NOTE_PREFIX = (
    "[Tool-loop context note: earlier tool results were summarized to fit this tier's context "
    "window. Summary follows.]\n"
)


def _tier_num_ctx(tier: Any) -> Optional[int]:
    """The tier's effective context window: fleet_config num_ctx, which backends/ollama.py
    sends as options.num_ctx on every request. None when unknown -> no budget logic."""
    try:
        from config import TIER_BACKENDS
        n = int((TIER_BACKENDS.get(tier) or {}).get("num_ctx") or 0)
        return n if n > 0 else None
    except Exception:  # noqa: BLE001 -- a lookup failure must never break the loop
        return None


def _result_cap_chars(num_ctx: Optional[int]) -> int:
    if not num_ctx:
        return _RESULT_CAP_MAX
    return max(2048, min(_RESULT_CAP_MAX, int(num_ctx * _RESULT_CTX_SHARE * _CHARS_PER_TOKEN)))


def _output_reserve(num_ctx: int, max_tokens: Optional[int]) -> int:
    want = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 1024
    return min(want, num_ctx // 4)


def _last_tool_block_start(msgs: list[dict]) -> int:
    """Index of the newest assistant message that carries tool_calls (the latest tool block)."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and msgs[i].get("tool_calls"):
            return i
    return len(msgs)


def _prune_old_tool_results(msgs: list[dict], keep_from: int) -> int:
    """Cut tool results before keep_from to head+tail. Replaces the dicts (never mutates the
    caller's objects). Returns how many were cut."""
    limit = _PRUNE_HEAD + _PRUNE_TAIL + len(_PRUNE_MARKER)
    n = 0
    for i in range(keep_from):
        m = msgs[i]
        c = m.get("content")
        if m.get("role") == "tool" and isinstance(c, str) and len(c) > limit:
            msgs[i] = {**m, "content": c[:_PRUNE_HEAD] + _PRUNE_MARKER + c[-_PRUNE_TAIL:]}
            n += 1
    return n


async def _compact_with_summary(backend_dispatch_fn, tier, msgs, keep_from, temperature,
                                caller_thinking, request_id) -> bool:
    """Summarize the tool work between the task and the latest tool block, in place.
    Returns False (msgs untouched) when there is nothing to summarize or the call fails."""
    head_end = next((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), len(msgs))
    if head_end >= keep_from:
        return False
    try:
        r = await backend_dispatch_fn(
            tier=tier,
            messages=msgs[:keep_from] + [{"role": "user", "content": _COMPACT_INSTRUCTION}],
            stream=False,
            temperature=temperature,
            max_tokens=_SUMMARY_MAX_TOKENS,
            caller_thinking=caller_thinking,
            tools=None,
            tool_choice=None,
        )
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as e:  # noqa: BLE001
        log.warning(f"tool_loop compaction summary failed request_id={request_id} err={type(e).__name__}")
        return False
    text = text.strip()
    if not text:
        return False
    note = {"role": "user", "content": _COMPACT_NOTE_PREFIX + text[:_SUMMARY_MAX_CHARS]}
    msgs[:] = msgs[:head_end] + [note] + msgs[keep_from:]
    return True


# ---------------------------------------------------------------------------
# Ceremony 4 Pattern 3 (2026-09-11): paged tool output, a cap summary, a doom-loop hash.
# (OpenCode tool/truncate.ts:85-141, session/prompt.ts:1281, session/processor.ts:356-379.)
#   1. A tool result over the per-result cap is kept WHOLE in this loop's _page_store (memory
#      only: no disk path in the pod, gone when the loop returns). The model gets a hint naming
#      tool_output_page, then the head and the tail. tool_output_page is served IN-PROCESS and
#      never reaches the gateway. It is advertised only once a page exists, so a loop that never
#      pages sends byte-identical tools. Policy class READ (bifrost_shell_policy.json); if the
#      policy does not grant it, the loop falls back to P1's plain cut.
#   2. At the iteration cap, with at least one successful tool call, ONE last call is made with
#      no tools, and the model's handoff is returned (reason "cap_summary") instead of the
#      synthetic apology. Any failure keeps the old terminal.
#   3. A call whose (name, canonical args) hash equals the previous two calls is refused with an
#      error result instead of executed.
# ---------------------------------------------------------------------------
PAGE_TOOL_NAME = "tool_output_page"
_PAGE_STORE_MAX_CHARS = 1_000_000   # per loop; the oldest pages are evicted first (the pod is 1Gi)
_DOOM_REPEAT = 3
_CAP_SUMMARY_INSTRUCTION = (
    "The tool-call budget for this request is exhausted; no more tools can be called. Using only "
    "the tool results above, state: what was found, with file names, line numbers and values "
    "exactly as the tools returned them; what is still unknown; and the next step to take."
)
_PAGE_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": PAGE_TOOL_NAME,
        "description": (
            "Read more of an earlier tool result that was too long to show in full. Use it ONLY "
            "with an id from a '[tool result truncated ... tool_output_page(id=...)]' note. It "
            "does NOT read files: use read_file or git_read for files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The id from the truncation note, e.g. r2_0."},
                "offset": {"type": "integer", "description": "Character offset to read from.", "default": 0},
                "limit": {"type": "integer", "description": "Characters to return (capped by the loop)."},
            },
            "required": ["id"],
        },
    },
}


def _page_text(full: str, rid: str, cap: int) -> str:
    """A note naming the page id FIRST (P1's head-keeping prune preserves it), then the head,
    an offset marker and the tail. At most `cap` chars; the caller only pages len(full) > cap."""
    tail = max(256, cap // 8)
    top = (f"[tool result truncated: {len(full)} chars in full. The rest is kept for this request; "
           f"read it with {PAGE_TOOL_NAME}(id='{rid}', offset=N).]\n")
    head = max(512, cap - tail - len(top) - 96)
    mid = f"\n[... {len(full) - head - tail} chars omitted; continue at offset={head} ...]\n"
    return top + full[:head] + mid + full[-tail:]


def _page_store_put(store: dict, rid: str, full: str) -> None:
    """Insert, then evict the oldest pages until the store fits _PAGE_STORE_MAX_CHARS."""
    store[rid] = full[:_PAGE_STORE_MAX_CHARS]
    while len(store) > 1 and sum(len(v) for v in store.values()) > _PAGE_STORE_MAX_CHARS:
        store.pop(next(iter(store)))


def _page_read(store: dict, args: dict, cap: int) -> dict:
    """Serve tool_output_page from the store. The limit is capped at cap // 2 so that the
    JSON-escaped reply still fits the per-result cap."""
    rid = str(args.get("id", ""))
    if rid not in store:
        return {"ok": False, "kind": "page_not_found",
                "error": f"no stored tool output with id '{rid}' in this request (ids come from a "
                         "truncation note; the oldest pages may have been evicted)"}
    full = store[rid]
    max_limit = max(256, cap // 2)
    try:
        offset = max(0, int(args.get("offset") or 0))
        limit = int(args.get("limit") or max_limit)
    except (TypeError, ValueError):
        offset, limit = 0, max_limit
    limit = max(1, min(limit, max_limit))
    text = full[offset: offset + limit]
    nxt = offset + len(text)
    return {"ok": True, "result": {"id": rid, "offset": offset, "returned": len(text), "total": len(full),
                                   "next_offset": nxt if nxt < len(full) else None, "text": text}}


def _call_hash(name: str, args: Any) -> str:
    try:
        canon = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        canon = repr(args)
    return hashlib.sha256(f"{name}\x00{canon}".encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# Igor review follow-up (2026-09-11; finding B6 of the 2026-09-10 harness review, OpenCode
# s.9.3): repair a malformed tool NAME only onto this role's allowlist, and answer an argument
# error with a structured tool result instead of running the call.
#   1. Arguments that did not parse used to become {} and the call RAN with no arguments.
#      Arguments that parsed to a non-object were passed on as they were (a list crashed the
#      delegate fence on args.get). Both now get kind "bad_arguments" and are NOT executed.
#      None, "" and whitespace still mean "no arguments".
#   2. For the tools this loop serves or fences ITSELF (_ARGS_CHECKED_IN_LOOP), a call missing
#      an argument its schema marks required, or giving it as null, gets the same error, naming
#      the missing and the expected arguments. A mode=tools delegate with no subtask used to run
#      a whole nested loop on an empty message. Gateway-served tools are not re-checked here:
#      the gateway validates their arguments against the tool signature itself. Types are not
#      checked anywhere in the loop.
#   3. A name that is not in the allowlist is retried stripped, without a "functions." /
#      "tools." prefix, lower-cased and with "-" -> "_". The result is used ONLY if it is in
#      the allowlist, so a repair can never reach a tool the role may not call.
# ---------------------------------------------------------------------------
_NAME_PREFIXES = ("functions.", "function.", "tools.", "tool.")
_ARGS_CHECKED_IN_LOOP = frozenset({"delegate_subtask", PAGE_TOOL_NAME})


def _mode_of(args: dict) -> str:
    """The delegate `mode`, defaulting to "reasoning".

    FIX 2026-09-26 (John: "any bug fixes"). `(args.get("mode") or "reasoning").lower()` raises
    AttributeError when a model sends `mode` as a list or a dict -- the same crash class as the
    unhashable leaf_alias fixed below, one line away from it. An unknown mode VALUE is already
    routed as "reasoning" (test_unknown_mode_value_is_routed_as_reasoning_without_the_key), so a
    non-string takes that same established path instead of raising out of the turn.
    """
    m = args.get("mode")
    return m.lower() if isinstance(m, str) and m else "reasoning"


def _repair_tool_name(name: Any, safe) -> Optional[str]:
    """The allowlisted tool `name` most likely meant, or None. Never widens: the answer is
    always a member of `safe`, and never the unchanged name."""
    if not isinstance(name, str):
        return None
    n = name.strip()
    for p in _NAME_PREFIXES:
        if n.lower().startswith(p):
            n = n[len(p):]
            break
    for cand in (n, n.lower().replace("-", "_")):
        if cand != name and cand in safe:
            return cand
    return None


def _tool_parameters(name: str, tools: list) -> Optional[dict]:
    """The JSON-schema `parameters` of an advertised tool, or None if it is not advertised."""
    for d in tools or []:
        f = d.get("function") or {}
        if f.get("name") == name:
            return f.get("parameters") or {}
    if name == PAGE_TOOL_NAME:            # advertised only after the first page
        return _PAGE_TOOL_DEFINITION["function"]["parameters"]
    return None


def _arguments_problem(name: str, args: dict, tools: list) -> Optional[str]:
    """A shape-only description of what is wrong with `args` (argument NAMES, never values)."""
    params = _tool_parameters(name, tools)
    if not params:
        return None
    missing = [k for k in (params.get("required") or []) if args.get(k) is None]
    if missing:
        return "missing required argument(s): " + ", ".join(missing)
    return None


def _expected_arguments(name: str, tools: list) -> dict:
    params = _tool_parameters(name, tools) or {}
    required = list(params.get("required") or [])
    return {"required": required,
            "optional": sorted(k for k in (params.get("properties") or {}) if k not in required)}


async def _cap_summary(backend_dispatch_fn, tier, msgs, temperature, caller_thinking, request_id,
                       num_ctx) -> Optional[str]:
    """ONE no-tools call for a handoff at the iteration cap. None on a failed call, an empty
    reply, a reply that still tries to call tools, or a prompt that would not fit the window."""
    ask = msgs + [{"role": "user", "content": _CAP_SUMMARY_INSTRUCTION}]
    if num_ctx:
        _prune_old_tool_results(ask, _last_tool_block_start(ask))   # replaces entries of the copy only
        if _estimate_tokens(ask, None) > num_ctx - _output_reserve(num_ctx, _SUMMARY_MAX_TOKENS):
            log.warning(f"tool_loop cap summary skipped: prompt over the window request_id={request_id}")
            return None
    try:
        r = await backend_dispatch_fn(
            tier=tier,
            messages=ask,
            stream=False,
            temperature=temperature,
            max_tokens=_SUMMARY_MAX_TOKENS,
            caller_thinking=caller_thinking,
            tools=None,
            tool_choice=None,
        )
        m = (r.get("choices") or [{}])[0].get("message") or {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"tool_loop cap summary failed request_id={request_id} err={type(e).__name__}")
        return None
    if m.get("tool_calls"):
        return None
    text = (m.get("content") or "").strip()
    return text or None


# ---------------------------------------------------------------------------
# Pattern 2 (2026-09-11, John: option A): backend-error recovery + opt-in escalation.
#   1. A failed backend dispatch is CLASSIFIED (_classify_backend_error) instead of ending the loop
#      blind: TRANSPORT, TIMEOUT, SERVER (5xx), RATE_LIMIT (429), WEDGED (degenerate.py's empty/wedged
#      reply), CONTEXT (400/413 naming the context window), BAD_REQUEST (other 4xx), OTHER.
#   2. The loop recovers on the SAME tier at most _RECOVERY_BUDGET times per loop, and only where
#      nothing below it already retried. backends/_retry.py retries openai_compat and anthropic on
#      429/503/504, timeouts and transport errors (4 attempts); backends/ollama.py retries nothing.
#        ollama                       TRANSPORT, TIMEOUT, SERVER, WEDGED  -> retry after a short pause
#        openai_compat / anthropic    SERVER 500 or 502 only              -> retry after a short pause
#        any tier                     CONTEXT                             -> prune + one summary, retry
#        unknown tier type            no recovery (the behavior before Pattern 2)
#      A retry is skipped when the pause would outlive total_turn_timeout_s.
#   3. If the failure survives and the caller passed raise_on_backend_error=True, the loop raises
#      ToolLoopBackendError, so the caller's cascade moves to the next tier (main.py's INTERACTIVE
#      loop opts in; its `except Exception` is the hop). Default False keeps the synthetic
#      backend_error terminal: subtask_graph's plain-call fallback and the leaf recursion rely on it.
#   4. Telemetry: _bifrost_tool_loop.error_code (last classified failure, None if none) and
#      .recoveries on both exits; a TOOL_LOOP_RECOVER log line per recovery action.
_RECOVERY_BUDGET = 2
_RECOVERY_PAUSE_S = 1.0
_CONTEXT_MARKERS = (
    "context length", "context_length", "maximum context", "context window",
    "prompt is too long", "too many tokens", "exceeds the context",
)
# A delegated leaf (mode="tools") succeeded only if its loop ended with a real answer, or with
# P3's handoff at the cap. Every other reason means its content is the loop's synthetic apology.
_LEAF_SUCCESS_REASONS = ("completed", "cap_summary")

# SAFE_LEAVES allowlist for mode="tools" (2026-09-11). The in-process leaf recursion below
# resolved leaf_alias through tier_resolver (main.py's alias->Tier pin table). That table pins
# cloud tiers and candidate/duplicate aliases, so a model could run a nested tool loop on
# bifrost-cerebras, bifrost-reasoning, bifrost-vision, bifrost-igor, ... -- any pinned alias
# whose registry entry did not say tool_calls:false. The allowlist has ONE source: SAFE_LEAVES
# in bifrost_delegate.py, which bifrost_delegate.delegate() and bifrost_shell.delegate_subtask
# enforce too. Read with ast, NOT imported: importing bifrost_delegate GETs /v1/models and builds
# a FastMCP server. Re-read on every mode="tools" call, so a SAFE_LEAVES edit needs no restart.
# FAIL-CLOSED: if the file is missing or SAFE_LEAVES cannot be read, EVERY mode="tools" leaf is
# refused. The Router image ships it at /app/bifrost_delegate.py (Dockerfile COPY . .).
_SAFE_LEAVES_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bifrost_delegate.py")


def _safe_leaves():
    """(frozenset of SAFE_LEAVES keys, "") or (None, reason). Never raises."""
    import ast as _ast_sl
    try:
        with open(_SAFE_LEAVES_SRC, encoding="utf-8") as fh:
            tree = _ast_sl.parse(fh.read())
        for node in tree.body:
            if isinstance(node, _ast_sl.Assign) and any(
                    isinstance(t, _ast_sl.Name) and t.id == "SAFE_LEAVES" for t in node.targets):
                table = _ast_sl.literal_eval(node.value)
                if isinstance(table, dict) and table and all(isinstance(k, str) for k in table):
                    return frozenset(table), ""
                return None, "SAFE_LEAVES is not a non-empty dict with str keys"
        return None, "no SAFE_LEAVES assignment found"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}"


class ToolLoopBackendError(RuntimeError):
    """A backend failure that survived the loop's recovery, raised only when the caller passed
    raise_on_backend_error=True. .code is the _classify_backend_error code."""

    def __init__(self, code: str, tier: Any, recoveries: int, cause: BaseException):
        self.code = code
        self.tier = tier
        self.recoveries = recoveries
        super().__init__(
            f"tool loop backend error code={code} tier={getattr(tier, 'value', tier)} "
            f"recoveries={recoveries}: {type(cause).__name__}"
        )


def _http_status(e: BaseException) -> Optional[int]:
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code is None:
        code = getattr(e, "status_code", None)   # fastapi HTTPException shape
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _error_text(e: BaseException) -> str:
    parts = [str(e), str(getattr(e, "detail", "") or "")]
    try:
        resp = getattr(e, "response", None)
        if resp is not None:
            parts.append(resp.text[:2000])
    except Exception:  # noqa: BLE001 -- an unread/streamed body has no text
        pass
    return " ".join(parts).lower()


def _classify_backend_error(e: BaseException) -> str:
    try:
        from degenerate import WedgedBackendError
        if isinstance(e, WedgedBackendError):
            return "WEDGED"
    except Exception:  # noqa: BLE001
        pass
    if isinstance(e, httpx.TimeoutException):   # before TransportError: it is a subclass
        return "TIMEOUT"
    if isinstance(e, httpx.TransportError):
        return "TRANSPORT"
    status = _http_status(e)
    if status is not None:
        if status == 429:
            return "RATE_LIMIT"
        if status >= 500:
            return "SERVER"
        if status == 413 or (status == 400 and any(m in _error_text(e) for m in _CONTEXT_MARKERS)):
            return "CONTEXT"
        if 400 <= status < 500:
            return "BAD_REQUEST"
    if any(m in _error_text(e) for m in _CONTEXT_MARKERS):
        return "CONTEXT"
    return "OTHER"


def _backend_type(tier: Any) -> Optional[str]:
    try:
        from config import TIER_BACKENDS
        return (TIER_BACKENDS.get(tier) or {}).get("type")
    except Exception:  # noqa: BLE001 -- a lookup failure must never break the loop
        return None


def _recovery_action(code: str, backend_type: Optional[str], status: Optional[int]) -> Optional[str]:
    if code == "CONTEXT":
        return "compact"
    if backend_type == "ollama" and code in ("TRANSPORT", "TIMEOUT", "SERVER", "WEDGED"):
        return "retry"
    if backend_type in ("openai_compat", "anthropic") and code == "SERVER" and status in (500, 502):
        return "retry"
    return None


def _terminal_response(
    last_response: Optional[dict],
    terminal_text: str,
    reason: str,
    request_id: str,
) -> dict:
    """Build a synthetic terminal response that is escalation-safety-clean
    (plain assistant content, finish_reason=stop, no error/refusal markers
    that could trigger SEC-2 cascade escalation when returned into the
    main.py cascade loop). Carries _bifrost_tool_loop telemetry."""
    base = last_response if last_response else {
        "id": f"loop-terminal-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "bifrost-igor",
        "choices": [{"index": 0, "message": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    try:
        base["choices"][0]["message"] = {"role": "assistant", "content": terminal_text}
        base["choices"][0]["finish_reason"] = "stop"
    except (KeyError, IndexError, TypeError):
        base["choices"] = [{"index": 0, "message": {"role": "assistant", "content": terminal_text}, "finish_reason": "stop"}]
    base.setdefault("_bifrost_tool_loop", {})["reason"] = reason
    return base


async def run_tool_loop(
    *,
    backend_dispatch_fn: Callable[..., Awaitable[dict]],
    tier: Any,
    messages: list[dict],
    temperature: float,
    max_tokens: Optional[int],
    caller_thinking: bool,
    gateway_call_fn: GatewayCallFn,
    max_iterations: int = 4,
    total_turn_timeout_s: float = 120.0,
    request_id: str = "",
    role: str = "igor",
    depth: int = 0,
    tier_resolver: Optional[Callable[[str], Any]] = None,
    raise_on_backend_error: bool = False,
) -> dict:
    """Run the model<->tool iteration loop for a caller ROLE (B2).

    depth (B2 Option B fence): the delegation depth of THIS loop. 0 = top caller (CC/Igor).
        delegate_subtask is refused fail-closed when depth >= MAX_DELEGATE_DEPTH, and the
        surplus past DELEGATE_WIDTH_CAP per turn is refused; every delegate attempt is
        telemetered with role+depth. Igor runs at depth 0 -> the fence is inert for Igor.

    role (B2): the caller role -- "igor" (default, byte-unchanged: the module globals)
        or "leaf" (the same gate: Option B [John 2026-06-27] -- leaf MAY delegate,
        depth-bounded; an UNKNOWN role fail-closes to READ/ANALYZE, no delegate_subtask). The role scopes the
        advertised tools, the allowlist gate, the breach canary, and the telemetry. The
        endpoint/alias are carried by backend_dispatch_fn (the closure) + tier (caller id),
        so the SAME engine/gate/breach/telemetry serve every caller -- no second surface.

    Args:
        backend_dispatch_fn: same signature as main.dispatch_to_tier. The loop
            calls it with stream=False on each iteration and passes the safe
            tool definitions + tool_choice="auto".
        tier: the Tier enum value (forwarded to backend_dispatch_fn).
        messages: list of OpenAI-shape messages, AFTER IGOR_BASELINE injection
            and any RAG augmentation. The loop appends to a COPY; the caller's
            list is not mutated.
        gateway_call_fn: closure returned by make_gateway_caller, or a test mock.
        max_iterations: doom-loop break. Default 4 (single-tool = 2 iters;
            2-step chain = 3 iters; one round of headroom).
        total_turn_timeout_s: wall-clock budget for the whole loop.
        request_id: for log correlation; appears in shape-only audit log lines.
        raise_on_backend_error: Pattern 2 opt-in (default False). When a backend failure
            survives the loop's own recovery, raise ToolLoopBackendError instead of
            returning the backend_error terminal, so the caller's cascade can hop.

    Returns:
        OpenAI-shape chat completion dict. On terminal conditions (cap hit,
        timeout, all calls failed, backend raised) returns a synthetic
        terminal response with _bifrost_tool_loop.reason set; never raises, except
        ToolGateBreach and (Pattern 2, opt-in) ToolLoopBackendError.
    """
    tools = get_safe_tool_definitions(role)
    _safe = _safe_for_role(role)                 # B2: role-scoped allowlist gate (see _safe_for_role)
    _exec_allowed = _executable_for_role(role)   # B2: role-scoped breach canary set
    t_start = time.perf_counter()
    msgs = list(messages)  # never mutate caller's list

    # 2026-06-19 refusal fix (cause-a model-bias): the model was self-answering lint/type
    # questions instead of calling run_ruff/run_mypy/run_checks (made those tools
    # uncertifiable). Nudge tool-use; inserted AFTER any leading IGOR_BASELINE system
    # message so baseline ordering is preserved. (Telemetry below makes invocation PROVABLE
    # regardless of whether the model complies.)
    _tool_directive = {
        "role": "system",
        "content": (
            "When the task asks you to lint, type-check, or validate Python code, you MUST "
            "call the run_ruff / run_mypy / run_checks tool and report ONLY the tool's result. "
            "Do not analyze the code from your own knowledge."
        ),
    }
    _ins = 1 if (msgs and msgs[0].get("role") == "system") else 0
    msgs.insert(_ins, _tool_directive)

    last_response: Optional[dict] = None
    successful_tool_calls = 0
    delegate_count = 0  # B2 Option B fence: per-turn delegate_subtask width counter
    forge_delegate_count = 0  # per-turn Forge-hosted leaf counter (RAM-bus cap, 2026-09-25)
    tools_called: list[str] = []  # 2026-06-19 telemetry: names of tools actually executed
    terminal_reason: Optional[str] = None
    iteration = 0
    _ctx = _tier_num_ctx(tier)        # Ceremony 4 P1: None -> no budget logic (today's behavior)
    _cap = _result_cap_chars(_ctx)
    _compactions = 0
    _pruned_total = 0
    _est_peak = 0
    _length_stops = 0                 # Ceremony 4 P4
    _page_store: dict[str, str] = {}  # Ceremony 4 P3: this loop's memory only
    _page_ok = PAGE_TOOL_NAME in _safe  # policy grants it -> page; else P1's plain cut
    _pages = 0
    _page_reads = 0
    _doom_refusals = 0
    _recent_calls: list[str] = []
    _recoveries = 0                     # Pattern 2: same-tier recovery actions used this loop
    _error_code: Optional[str] = None   # Pattern 2: last classified backend failure
    _backend_exc: Optional[BaseException] = None

    for iteration in range(max_iterations):
        if time.perf_counter() - t_start > total_turn_timeout_s:
            terminal_reason = "loop_timeout"
            log.warning(
                f"tool_loop timeout request_id={request_id} "
                f"elapsed={time.perf_counter() - t_start:.1f}s iteration={iteration}"
            )
            break

        # Ceremony 4 P1: in-loop context budget (see the block above _terminal_response).
        if _ctx and iteration > 0:
            _budget = _ctx - _output_reserve(_ctx, max_tokens)
            _est = _estimate_tokens(msgs, tools)
            _est_peak = max(_est_peak, _est)
            if _est >= _COMPACT_AT * _budget:
                _keep = _last_tool_block_start(msgs)
                _n_pruned = _prune_old_tool_results(msgs, _keep)
                _pruned_total += _n_pruned
                _est_after = _estimate_tokens(msgs, tools)
                _summarised = False
                if _est_after >= _COMPACT_AT * _budget and _compactions == 0:
                    _compactions += 1
                    _summarised = await _compact_with_summary(
                        backend_dispatch_fn, tier, msgs, _keep, temperature, caller_thinking, request_id)
                    _est_after = _estimate_tokens(msgs, tools)
                log.info(
                    f"TOOL_LOOP_COMPACT: request_id={request_id} iter={iteration} ctx={_ctx} "
                    f"budget={_budget} est={_est}->{_est_after} pruned={_n_pruned} summarized={_summarised}"
                )
                if _est_after > _budget:
                    terminal_reason = "context_budget"
                    log.warning(
                        f"tool_loop context budget exceeded request_id={request_id} iter={iteration} "
                        f"ctx={_ctx} est={_est_after} budget={_budget}"
                    )
                    break

        # Pattern 2: classify a failure and recover on this tier (see the block above
        # _terminal_response); a failure that survives ends the loop or, opt-in, raises.
        while True:
            try:
                response = await backend_dispatch_fn(
                    tier=tier,
                    messages=msgs,
                    stream=False,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    caller_thinking=caller_thinking,
                    tools=tools,
                    tool_choice="auto",
                )
                break
            except Exception as e:
                _error_code = _classify_backend_error(e)
                log.warning(
                    f"tool_loop backend dispatch raised request_id={request_id} "
                    f"iter={iteration} err={type(e).__name__} code={_error_code}"
                )
                _action = _recovery_action(_error_code, _backend_type(tier), _http_status(e))
                if _action == "compact" and _recoveries < _RECOVERY_BUDGET:
                    _keep = _last_tool_block_start(msgs)
                    _n_pruned = _prune_old_tool_results(msgs, _keep)
                    _pruned_total += _n_pruned
                    _summarised = False
                    if _compactions == 0:
                        _compactions += 1
                        _summarised = await _compact_with_summary(
                            backend_dispatch_fn, tier, msgs, _keep, temperature, caller_thinking, request_id)
                    if _n_pruned or _summarised:
                        _recoveries += 1
                        log.warning(
                            f"TOOL_LOOP_RECOVER: request_id={request_id} iter={iteration} "
                            f"tier={getattr(tier, 'value', tier)} code={_error_code} action=compact "
                            f"pruned={_n_pruned} summarized={_summarised} n={_recoveries}"
                        )
                        continue
                elif (_action == "retry" and _recoveries < _RECOVERY_BUDGET
                      and total_turn_timeout_s - (time.perf_counter() - t_start) > _RECOVERY_PAUSE_S):
                    _recoveries += 1
                    log.warning(
                        f"TOOL_LOOP_RECOVER: request_id={request_id} iter={iteration} "
                        f"tier={getattr(tier, 'value', tier)} code={_error_code} action=retry n={_recoveries}"
                    )
                    await asyncio.sleep(_RECOVERY_PAUSE_S)
                    continue
                _backend_exc = e
                break
        if _backend_exc is not None:
            terminal_reason = "backend_error"
            if raise_on_backend_error:
                log.warning(
                    f"TOOL_LOOP_ESCALATE: request_id={request_id} tier={getattr(tier, 'value', tier)} "
                    f"code={_error_code} recoveries={_recoveries} -> caller cascade"
                )
                raise ToolLoopBackendError(_error_code or "OTHER", tier, _recoveries, _backend_exc) from _backend_exc
            break

        last_response = response

        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            terminal_reason = "malformed_backend_response"
            break

        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # 2026-06-19 telemetry (keystone): surface tool-invocation on the normal-completion
            # path so the CALLER can PROVE whether a tool actually ran (lint tools were
            # uncertifiable because a coder model self-produces the same answer). Empty
            # tools_called here = the model answered WITHOUT calling a tool.
            response.setdefault("_bifrost_tool_loop", {}).update({
                "tools_called": tools_called,
                "successful_tool_calls": successful_tool_calls,
                "iterations": iteration + 1,
                "reason": "completed",
                "error_code": _error_code,
                "recoveries": _recoveries,
                "compactions": _compactions,
                "pruned_results": _pruned_total,
                "ctx_len": _ctx,
                "est_tokens_peak": _est_peak,
                "length_stops": _length_stops,
                "pages": _pages,
                "page_reads": _page_reads,
                "doom_refusals": _doom_refusals,
            })
            return response

        msgs.append({
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": tool_calls,
        })

        # Ceremony 4 P4 (Pi agent-loop.ts:227-232): a reply the BACKEND cut off at its output
        # token limit may carry truncated tool-call arguments -- a shortened path or query that
        # parses fine and runs the wrong call. Execute none of them; answer each with an error
        # and let the model re-issue. reasoning_coalesce rewrites "stop" -> "length" for an
        # unclosed <think> block and keeps the backend's value in _bifrost_original_finish_reason;
        # that value wins, because only the backend knows whether the arguments were cut.
        _choice0 = response["choices"][0]
        _backend_fr = _choice0.get("_bifrost_original_finish_reason") or _choice0.get("finish_reason")
        if _backend_fr == "length":
            _length_stops += 1
            for tc in tool_calls:
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "call_unknown"),
                    "content": json.dumps({"error": _LENGTH_STOP_ERROR, "kind": "length_stop"}),
                })
            log.warning(
                f"TOOL_LOOP_LENGTH_STOP: request_id={request_id} iter={iteration} tier={tier} "
                f"calls={len(tool_calls)} refused (reply hit the output token limit)"
            )
            continue

        # v203 PARALLEL DISPATCH (three-phase). Phase 1: SEQUENTIAL gate + delegate fence
        # (order-dependent counters + telemetry, NO I/O) -> per-call plan. Phase 2: asyncio.gather
        # ONLY the I/O dispatches -> genuine multi-endpoint concurrency. Phase 3: SEQUENTIAL post --
        # the BREACH canary stays a HARD raise HERE (NOT inside a gathered coroutine, where
        # return_exceptions would SWALLOW a security halt) + success/telemetry/append IN ORDER.
        _plan = []
        for tc in tool_calls:
            tc_id = tc.get("id", "call_unknown")
            func = tc.get("function") or {}
            name = func.get("name", "")
            args_raw = func.get("arguments", {})
            # Igor review B6 (2026-09-11; see _repair_tool_name): arguments that do not parse, or
            # parse to a non-object, get an error result and are NOT executed. They used to become
            # {} (or pass through as a list) and the call ran anyway.
            _args_error = None
            if args_raw is None or (isinstance(args_raw, str) and not args_raw.strip()):
                args = {}
            elif isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError as _jde:
                    args, _args_error = {}, f"arguments are not valid JSON ({_jde.msg} at char {_jde.pos})"
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = args_raw
            if _args_error is None and not isinstance(args, dict):
                _args_error = f"arguments must be a JSON object, got {type(args).__name__}"
                args = {}

            if not name:
                _plan.append({"tc_id": tc_id, "kind": "msg",
                              "content": json.dumps({"error": "tool_call missing name", "kind": "parse"})})
                continue

            if name not in _safe:
                _fixed = _repair_tool_name(name, _safe)
                if _fixed is not None:
                    log.warning(f"TOOL_LOOP_NAME_REPAIRED: request_id={request_id} iter={iteration} "
                                f"to={_fixed} from_len={len(name)}")
                    name = _fixed

            if name not in _safe:
                _emit_violation(_violation_record(tier, name, "ATTEMPT_REFUSED", args, role))
                _plan.append({"tc_id": tc_id, "kind": "msg",
                              "content": json.dumps({"error": f"tool '{name}' not in safe-subset allowlist", "kind": "out_of_subset"})})
                continue

            if _args_error is None and name in _ARGS_CHECKED_IN_LOOP:
                _args_error = _arguments_problem(name, args, tools)
            if _args_error is not None:
                log.warning(f"TOOL_LOOP_BAD_ARGS: request_id={request_id} iter={iteration} tool={name} "
                            f"problem={_args_error.split(' (')[0]}")
                _plan.append({"tc_id": tc_id, "kind": "msg",
                              "content": json.dumps({"error": f"tool '{name}': {_args_error}. The call was NOT executed; re-issue it with valid arguments.",
                                                     "kind": "bad_arguments",
                                                     "expected": _expected_arguments(name, tools)})})
                continue

            # Ceremony 4 P3: the same call (name + canonical args) as the previous two -> refuse it.
            _recent_calls.append(_call_hash(name, args))
            if len(_recent_calls) >= _DOOM_REPEAT and len(set(_recent_calls[-_DOOM_REPEAT:])) == 1:
                _doom_refusals += 1
                log.warning(f"TOOL_LOOP_DOOM_LOOP: request_id={request_id} iter={iteration} tool={name} "
                            f"repeated={_DOOM_REPEAT}")
                _plan.append({"tc_id": tc_id, "kind": "msg",
                              "content": json.dumps({"error": f"identical call to '{name}' repeated {_DOOM_REPEAT} times in a row; it was NOT executed. Change the arguments or answer with what you have.", "kind": "doom_loop"})})
                continue

            # Ceremony 4 P3: tool_output_page is served from this loop's page store, in-process.
            # Ceremony 6: the halt is consulted HERE because this branch `continue`s
            # before the gateway dispatch, so gated_tool -- and with it bifrost_halt --
            # never runs for this tool. Without this, a halt would stop every other
            # gateway tool and not
            # this one. An absent module or a raising halted() both mean "not halted",
            # the same fail-open bifrost_shell._gate_denies already chooses; the policy
            # remains the authority either way.
            if name == PAGE_TOOL_NAME:
                _halted = False
                if _bifrost_halt is not None:
                    try:
                        _halted = bool(_bifrost_halt.halted())
                    except Exception:                       # noqa: BLE001
                        _halted = False
                if _halted:
                    log.warning(f"TOOL_LOOP_HALTED: request_id={request_id} "
                                f"iter={iteration} tool={name}")
                    _plan.append({"tc_id": tc_id, "kind": "msg",
                                  "content": json.dumps({
                                      "error": "tool calls are HALTED; tool_output_page "
                                               "was not served. The halt is operator-"
                                               "controlled; do not retry, answer with "
                                               "what you already have.",
                                      "kind": "halted"})})
                    continue
                _page_reads += 1
                _plan.append({"tc_id": tc_id, "kind": "result", "name": name, "args": args,
                              "result": _page_read(_page_store, args, _cap)})
                continue

            # B2 Option B fence (SEQUENTIAL -- counters can't race): bound recursive delegation.
            if name == "delegate_subtask":
                delegate_count += 1
                _mode = _mode_of(args)
                if depth >= MAX_DELEGATE_DEPTH:
                    _emit_violation(_violation_record(tier, name, "DELEGATE_DEPTH_REFUSED", args, role, depth))
                    _plan.append({"tc_id": tc_id, "kind": "msg",
                                  "content": json.dumps({"error": f"delegate refused: depth {depth} >= MAX_DELEGATE_DEPTH {MAX_DELEGATE_DEPTH}", "kind": "delegate_depth_bound"})})
                    continue
                if delegate_count > DELEGATE_WIDTH_CAP:
                    _emit_violation(_violation_record(tier, name, "DELEGATE_WIDTH_REFUSED", args, role, depth))
                    _plan.append({"tc_id": tc_id, "kind": "msg",
                                  "content": json.dumps({"error": f"delegate refused: width {delegate_count} > DELEGATE_WIDTH_CAP {DELEGATE_WIDTH_CAP}", "kind": "delegate_width_cap"})})
                    continue
                # FIX 2026-09-26: a non-string leaf_alias (list/dict) is UNHASHABLE, so this
                # membership test raised TypeError and the exception escaped the turn.
                # tests/test_tool_loop_leaf_allowlist.py::test_non_string_leaf_refused_and_never_raises
                # already documents the intent -- its own comment reads "before the fix a list/dict
                # raised TypeError" -- and the fix was never made, so that test has been failing.
                # A non-string can never NAME a Forge leaf, so it skips this host cap and falls
                # through to the leaf allowlist guard, which refuses it as leaf_not_safe. Refused,
                # not raised, which is what the guard is for.
                _leaf_for_host_cap = args.get("leaf_alias", "")
                if isinstance(_leaf_for_host_cap, str) and _leaf_for_host_cap in _FORGE_LEAVES:
                    forge_delegate_count += 1
                    if forge_delegate_count > _FORGE_CONCURRENCY_CAP:
                        _emit_violation(_violation_record(tier, name,
                                                          "DELEGATE_FORGE_HOST_REFUSED",
                                                          args, role, depth, mode=_mode))
                        _plan.append({"tc_id": tc_id, "kind": "msg", "content": json.dumps({
                            "error": f"delegate refused: {forge_delegate_count} Forge-hosted leaves in one turn > _FORGE_CONCURRENCY_CAP {_FORGE_CONCURRENCY_CAP}. Phase 2 gathers these CONCURRENTLY and Forge serializes on one RAM bus (John 2026-09-17); dispatch Forge leaves in separate turns.",
                            "kind": "delegate_forge_host_cap"})})
                        continue
                _emit_violation(_violation_record(tier, name, "DELEGATE_PERMITTED", args, role, depth, mode=_mode))

            # Build a DISPATCH coroutine (or a pre-resolved result). NO awaits in phase 1.
            if name == "delegate_subtask" and _mode_of(args) == "tools":
                _leaf_alias = args.get("leaf_alias", "")
                # SAFE_LEAVES guard (2026-09-11): FIRST, before the registry lookup, the tier resolver
                # and any nested loop or model call. Exact match, no case/whitespace folding -- the
                # comparison bifrost_delegate.delegate() and bifrost_shell.delegate_subtask make. A
                # non-str leaf_alias is refused here too (tier_resolver raised TypeError on a list).
                _sl_set, _sl_why = _safe_leaves()
                if _sl_set is None or not isinstance(_leaf_alias, str) or _leaf_alias not in _sl_set:
                    if _sl_set is None:
                        _sl_err = (f"mode=tools refused: SAFE_LEAVES allowlist unavailable from "
                                   f"bifrost_delegate.py ({_sl_why}); refusing every leaf (fail-closed)")
                    else:
                        _sl_err = (f"mode=tools refused: leaf {_leaf_alias!r} is not in SAFE_LEAVES. "
                                   f"Pick one of: {sorted(_sl_set)}")
                    _plan.append({"tc_id": tc_id, "kind": "result", "name": name, "args": args,
                                  "result": {"ok": False, "error": _sl_err, "kind": "leaf_not_safe"}})
                    continue
                # v202 CAPABILITY-AWARE FAN-OUT: mode=tools requires a tool_calls:true leaf.
                _leaf_cap = None
                try:
                    import capability_registry as _capreg
                    _leaf_cap = _capreg.get_alias(_leaf_alias)
                except Exception:  # noqa: BLE001
                    _leaf_cap = None
                _leaf_tier = tier_resolver(_leaf_alias) if tier_resolver else None
                if _leaf_cap is not None and not _leaf_cap.get("tool_calls"):
                    _plan.append({"tc_id": tc_id, "kind": "result", "name": name, "args": args,
                                  "result": {"ok": False, "error": f"mode=tools refused: leaf '{_leaf_alias}' is tool_calls:false in the capability registry; use mode=reasoning for this leaf", "kind": "leaf_not_tool_capable"}})
                    continue
                if _leaf_tier is None:
                    _plan.append({"tc_id": tc_id, "kind": "result", "name": name, "args": args,
                                  "result": {"ok": False, "error": f"mode=tools: no tier for leaf '{_leaf_alias}'", "kind": "leaf_unresolved"}})
                    continue

                async def _leaf_dispatch(_alias=_leaf_alias, _ltier=_leaf_tier, _submsg=args.get("subtask", ""), _d=depth):
                    _sub = await run_tool_loop(
                        backend_dispatch_fn=backend_dispatch_fn,
                        tier=_ltier,
                        messages=[{"role": "user", "content": _submsg}],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        caller_thinking=caller_thinking,
                        gateway_call_fn=gateway_call_fn,
                        request_id=f"{request_id}:leaf{_d + 1}",
                        role="leaf",
                        depth=_d + 1,
                        tier_resolver=tier_resolver,
                    )
                    _ans = ""
                    try:
                        _ans = (_sub.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                    except Exception:  # noqa: BLE001
                        _ans = ""
                    # Pattern 2 leaf fix (2026-09-11): a leaf that GAVE UP carries the loop's synthetic
                    # apology as its content. Returned as ok:True, the parent counted the apology as a
                    # successful tool call and its model read it as the leaf's answer.
                    _sub_reason = (_sub.get("_bifrost_tool_loop") or {}).get("reason")
                    if _sub_reason not in _LEAF_SUCCESS_REASONS:
                        return {"ok": False, "kind": "leaf_gave_up",
                                "error": f"leaf '{_alias}' gave up without an answer (reason={_sub_reason}); "
                                         f"its reply is not a result"}
                    return {"ok": True, "result": {
                        "answer": _ans,
                        "trace": {
                            "mode": "tools", "role": "leaf", "depth": _d + 1, "leaf_alias": _alias,
                            "tools_called": (_sub.get("_bifrost_tool_loop") or {}).get("tools_called", []),
                        },
                    }}
                _plan.append({"tc_id": tc_id, "kind": "dispatch", "name": name, "args": args, "coro": _leaf_dispatch()})
            elif name == "web_search":
                # Ceremony 107 (John 2026-10-08, option b, igor only -- not leaf).
                # kind="dispatch", NOT "result": this is an HTTP round trip and phase 1
                # is documented "NO awaits in phase 1" above, so it joins the parallel
                # gather below instead of blocking. Default arguments bind the loop
                # variables, the same device _leaf_dispatch uses -- without them every
                # coroutine in a multi-call turn closes over the LAST iteration's args.
                # Ceremony 107 closure: web_search is served IN-PROCESS, so like
                # tool_output_page it never reaches gated_tool -- and therefore
                # neither bifrost_halt nor the bifrost-gates.toml overlay reaches it
                # unless the halt is consulted HERE, before the dispatch coroutine is
                # planned. Ceremony 6 established this for the page tool; a tool that
                # makes outbound requests needs it at least as much. The gap was
                # surfaced by tests/test_policy_tools_vs_gated_functions.py, whose
                # docstring states the property in as many words.
                _halted_web = False
                if _bifrost_halt is not None:
                    try:
                        _halted_web = bool(_bifrost_halt.halted())
                    except Exception:                       # noqa: BLE001
                        _halted_web = False
                if _halted_web:
                    log.warning(f"TOOL_LOOP_HALTED: request_id={request_id} "
                                f"iter={iteration} tool={name}")
                    _plan.append({"tc_id": tc_id, "kind": "msg",
                                  "content": json.dumps({
                                      "error": "tool calls are HALTED; web_search was "
                                               "not executed. The halt is operator-"
                                               "controlled; do not retry, answer with "
                                               "what you already have.",
                                      "kind": "halted"})})
                    continue
                async def _web_search(_q=args.get("query", ""),
                                      _n=args.get("max_results", 3)):
                    # Reuse the ENRICHMENT path's query builder rather than writing a
                    # second one. search_query.py was patched 2026-10-07 to strip
                    # bracketed tags and trailing assistant-directed clauses; a tool
                    # that built its own query would re-introduce that defect here.
                    from config import settings as _st
                    from search_query import build_search_query, pick_search_category
                    _query = build_search_query(_q)
                    if not _query:
                        return {"ok": False, "kind": "bad_arguments",
                                "error": "query reduced to empty after cleaning"}
                    _cat = pick_search_category(_q)
                    try:
                        _params = {"q": _query, "format": "json", "categories": _cat}
                        async with httpx.AsyncClient(timeout=20.0) as _c:
                            _r = await _c.get(f"{_st.searxng_url}/search", params=_params)
                            _r.raise_for_status()
                            _rows = (_r.json() or {}).get("results") or []
                    except Exception as _e:                       # noqa: BLE001
                        return {"ok": False, "kind": "transport",
                                "error": f"web_search failed: {type(_e).__name__}"}
                    try:
                        _cap_n = max(1, min(int(_n or 3), 10))
                    except (TypeError, ValueError):
                        _cap_n = 3
                    _out = [{"title": _x.get("title", ""), "url": _x.get("url", ""),
                             "snippet": (_x.get("content") or "")[:400]}
                            for _x in _rows[:_cap_n]]
                    # query_sent is returned deliberately: the cleaned query is what the
                    # engine actually saw. A nonce reaching SearXNG was only diagnosable
                    # on 2026-10-07 because the sent query was visible.
                    return {"ok": True, "result": {"query_sent": _query,
                                                   "category": _cat,
                                                   "results": _out}}
                _plan.append({"tc_id": tc_id, "kind": "dispatch", "name": name,
                              "args": args, "coro": _web_search()})
            else:
                # mode-arg fix (2026-09-11): `mode` is THIS loop's selector (reasoning vs tools), which
                # delegate_subtask's schema above advertises. bifrost_shell.delegate_subtask has no
                # `mode` parameter and FastMCP rejects an unexpected keyword (isError; the tool body
                # never runs), so a reasoning call that named its mode explicitly never reached the
                # leaf. Drop it from the GATEWAY arguments only; `args` (telemetry, doom-loop hash)
                # keeps it. Another tool's own `mode` (git_read: log/show/diff) is untouched.
                _gw_args = ({k: v for k, v in args.items() if k != "mode"}
                            if name == "delegate_subtask" else args)
                _plan.append({"tc_id": tc_id, "kind": "dispatch", "name": name, "args": args, "coro": gateway_call_fn(name, _gw_args, request_id)})

        # Phase 2: gather ONLY the dispatch coroutines (PARALLEL I/O); order preserved via index.
        _disp_idx = [i for i, p in enumerate(_plan) if p["kind"] == "dispatch"]
        if _disp_idx:
            _results = await asyncio.gather(*[_plan[i]["coro"] for i in _disp_idx], return_exceptions=True)
            # Pattern 2 leaf-breach fix (2026-09-11, John): a ToolGateBreach raised inside a delegated
            # leaf loop must halt THIS turn too. return_exceptions=True turned it into an ordinary
            # dispatch_error, so the leaf's canary fired but the parent carried on. The leaf already
            # emitted the violation record, the canary event and the alert; re-raise the same exception.
            for res in _results:
                if isinstance(res, ToolGateBreach):
                    log.critical(
                        f"TOOL_LOOP_LEAF_BREACH: request_id={request_id} role={role} depth={depth} "
                        f"-> halting this turn (the leaf already emitted the violation and canary event)"
                    )
                    raise res
            for i, res in zip(_disp_idx, _results):
                _plan[i]["kind"] = "result"
                if isinstance(res, Exception):
                    _plan[i]["result"] = {"ok": False, "error": f"{type(res).__name__}: {res}", "kind": "dispatch_error"}
                else:
                    _plan[i]["result"] = res

        # Phase 3: SEQUENTIAL post-process IN ORDER -- BREACH canary is a HARD raise here.
        for p in _plan:
            if p["kind"] == "msg":
                msgs.append({"role": "tool", "tool_call_id": p["tc_id"], "content": p["content"]})
                continue
            name = p["name"]
            args = p["args"]
            tc_id = p["tc_id"]
            tool_result = p["result"]

            if name not in _exec_allowed:
                _emit_violation(_violation_record(tier, name, "BREACH_EXECUTED", args, role))
                _emit_canary_event({
                    "invariant": "tool-gate-breach", "trigger": "EVENT",
                    "holds": False, "action": "raise_ToolGateBreach+halt",
                    "authority": "AUTO", "request_id": str(request_id),
                    **_violation_record(tier, name, "BREACH_EXECUTED", args, role),
                })
                _ha_notify_breach(name, request_id)
                raise ToolGateBreach(
                    f"denied/unknown tool '{name}' executed past gate (request_id={request_id})"
                )

            if tool_result is None:
                # 2026-07-30 ceremony igor-tool-loop-none-guard (operator-signed): gateway
                # caller returned None (observed: tool pod spawned with empty args exits 1;
                # caller yields None). Crashing here turned one bad tool call into a dead
                # leg/turn. Shape it as an error result; the existing error path reports it
                # to the model, which can retry or answer without the tool.
                tool_result = {"ok": False, "error": "gateway returned no result (tool spawn failed or timed out)", "kind": "gateway_none"}
            if tool_result.get("ok"):
                payload = tool_result["result"]
                successful_tool_calls += 1
                tools_called.append(name)  # 2026-06-19 telemetry
            else:
                payload = {
                    "error": tool_result.get("error", "unknown error"),
                    "kind": tool_result.get("kind", "unknown"),
                }

            result_str = json.dumps(payload)
            if len(result_str) > _cap and _page_ok and name != PAGE_TOOL_NAME:
                # Ceremony 4 P3: page it; the whole result stays readable via tool_output_page.
                _rid = f"r{iteration}_{_pages}"
                _pages += 1
                _page_store_put(_page_store, _rid, result_str)
                result_str = _page_text(result_str, _rid, _cap)
                if not any(d["function"]["name"] == PAGE_TOOL_NAME for d in tools):
                    tools = tools + [_PAGE_TOOL_DEFINITION]   # advertised from the first page on
            elif len(result_str) > _cap:        # Ceremony 4 P1: ctx-derived, never above 32 KB
                truncated = len(result_str) - _cap
                result_str = result_str[: _cap] + f"... [truncated {truncated} more bytes]"

            msgs.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_str,
            })

    # Reached terminal -- iteration cap, timeout, or backend error.
    if terminal_reason is None:
        terminal_reason = "iteration_cap"

    # Ceremony 4 P3: at the cap, with real tool results in hand, ask once for a handoff.
    _handoff = None
    if terminal_reason == "iteration_cap" and successful_tool_calls > 0:
        _handoff = await _cap_summary(backend_dispatch_fn, tier, msgs, temperature, caller_thinking,
                                      request_id, _ctx)
        if _handoff:
            terminal_reason = "cap_summary"

    if terminal_reason == "cap_summary":
        terminal_text = _handoff
    elif terminal_reason == "iteration_cap" and successful_tool_calls == 0 and _length_stops > 0:
        # Ceremony 4 P4: without this, N1 below would blame the tool transport.
        terminal_reason = "length_stop"
        terminal_text = (
            "The tool execution loop stopped because the model's replies kept hitting the "
            "output token limit, so none of its tool calls could be run safely. The request "
            "can be retried with a larger max_tokens or a narrower question."
        )
    elif terminal_reason == "iteration_cap" and successful_tool_calls == 0:
        # N1: distinguish tool-stack outage from model oscillation.
        terminal_reason = "no_successful_tool_exec"
        terminal_text = (
            "The tool execution loop reached its iteration limit without any tool "
            "calls succeeding. This may indicate a temporary transport issue between "
            "the router and the tool gateway. The partial conversation is preserved "
            "and the request can be retried."
        )
    elif terminal_reason == "iteration_cap":
        terminal_text = (
            "The tool execution loop reached its iteration limit before reaching a "
            "final answer. The partial results are preserved and the request can be "
            "retried with a more focused prompt."
        )
    elif terminal_reason == "loop_timeout":
        terminal_text = (
            "The tool execution loop exceeded its time budget. The partial results "
            "are preserved and the request can be retried."
        )
    elif terminal_reason == "context_budget":
        terminal_text = (
            "The tool execution loop stopped because the conversation no longer fits this "
            "tier's context window, even after compaction. The partial results are preserved "
            "and the request can be retried with a narrower question or on a larger-context tier."
        )
    elif terminal_reason == "backend_error":
        terminal_text = (
            "The tool execution loop encountered a backend dispatch error. The "
            "request can be retried."
        )
    else:
        terminal_text = (
            "The tool execution loop terminated unexpectedly. The request can be retried."
        )

    log.warning(
        f"tool_loop terminal request_id={request_id} reason={terminal_reason} "
        f"iters={iteration + 1} successful_tools={successful_tool_calls}"
    )

    _tr = _terminal_response(last_response, terminal_text, terminal_reason, request_id)
    _tr.setdefault("_bifrost_tool_loop", {}).update({
        "tools_called": tools_called,
        "successful_tool_calls": successful_tool_calls,
        "iterations": iteration + 1,
        "compactions": _compactions,
        "pruned_results": _pruned_total,
        "ctx_len": _ctx,
        "est_tokens_peak": _est_peak,
        "length_stops": _length_stops,
        "pages": _pages,
        "page_reads": _page_reads,
        "doom_refusals": _doom_refusals,
        "error_code": _error_code,
        "recoveries": _recoveries,
    })  # 2026-06-19 telemetry: invocation proof on terminal paths too
    return _tr

# ===========================================================================
# BUILD 1 (2026-07-01): async submit/collect JOB model -- the orchestration
# keystone. ADDITIVE ONLY: run_tool_loop and the sync delegate path above are
# BYTE-UNCHANGED. A multi-hop orchestration becomes a JOB so no single HTTP
# request is held past the ~120-130s Router upstream read-timeout ceiling:
# submit_job() writes a job record + spawns a tracked background worker and
# returns an id IMMEDIATELY (well under the ceiling); the worker runs the
# EXISTING run_tool_loop; collect_job() polls the record. Job STATE lives in a
# FILE (crash-survivable, worker-count-agnostic), never an in-process dict.
#
# Persistence: /app/logs/jobs/{job_id}.json. /app/logs is the bifrost-logs
# volume (hostPath /net-ssd/bifrost-logs), pod-writable (uid 1000), surfacing to
# Windows via the decomp_shadow node-bind. Env-overridable -- same idiom as
# _VIOLATION_LOG above.
#
# Fence (reuses the B2 Option B bound): a job at depth >= MAX_DELEGATE_DEPTH is
# REFUSED at submit (the async analog of the sync delegate depth gate); no more
# than _MAX_JOBS_IN_FLIGHT workers run at once (backpressure -- never spawn
# unbounded). depth threads INTO the job record so a sub-job chain stays bounded.
#
# Crash recovery: a pod restart kills in-flight workers; their records stay
# status=running with no live worker = STALE. _reconcile_stale_jobs() runs once,
# lazily, on the first job op after (re)start (no main.py startup hook needed --
# keeping this a single-file change) and marks any submitted/running record ->
# failed(worker_lost_on_restart). Chosen over re-queue: re-queue risks
# double-running a job whose side effects partially landed; the caller re-submits
# on seeing 'failed'.
# ===========================================================================

_JOBS_DIR = os.environ.get("BIFROST_JOBS_DIR", "/app/logs/jobs")
_MAX_JOBS_IN_FLIGHT = int(os.environ.get("BIFROST_MAX_JOBS_IN_FLIGHT", "8"))
_job_worker_tasks: set = set()   # strong refs: a bare create_task can be GC'd before it runs (main.py:193 idiom)
_jobs_reconciled = False         # lazy one-shot stale-job reconcile guard


def _job_path(job_id: str) -> str:
    return os.path.join(_JOBS_DIR, f"{job_id}.json")


def _job_write(record: dict) -> None:
    """Atomically persist a job record (write-temp + os.replace) so a crash
    mid-write never leaves a half-written record that collect_job would choke on."""
    os.makedirs(_JOBS_DIR, exist_ok=True)
    path = _job_path(record["job_id"])
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=True)
    os.replace(tmp, path)


def _job_read(job_id: str) -> Optional[dict]:
    try:
        with open(_job_path(job_id), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def _job_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _jobs_in_flight() -> int:
    """Count records still submitted/running. The FILE is the source of truth, so
    this survives a pod restart -- unlike len(_job_worker_tasks) (per-process)."""
    n = 0
    try:
        names = os.listdir(_JOBS_DIR)
    except FileNotFoundError:
        return 0
    for fn in names:
        if fn.endswith(".json"):
            rec = _job_read(fn[:-5])
            if rec and rec.get("status") in ("submitted", "running"):
                n += 1
    return n


def _reconcile_stale_jobs() -> int:
    """One-shot on the first job op after (re)start: a submitted/running record
    whose worker is gone (this process did not create it) is STALE -> mark failed.
    Never raises (best-effort; a reconcile failure must not break submit/collect)."""
    global _jobs_reconciled
    if _jobs_reconciled:
        return 0
    _jobs_reconciled = True
    marked = 0
    try:
        names = os.listdir(_JOBS_DIR)
    except FileNotFoundError:
        return 0
    for fn in names:
        if not fn.endswith(".json"):
            continue
        rec = _job_read(fn[:-5])
        if rec and rec.get("status") in ("submitted", "running"):
            rec["status"] = "failed"
            rec["error"] = "worker_lost_on_restart"
            rec["reconciled_at"] = _job_now_iso()
            try:
                _job_write(rec)
                marked += 1
            except OSError:
                pass
    if marked:
        log.warning(f"build1 reconcile: marked {marked} stale running-job(s) failed on restart")
    return marked


async def _run_job_worker(job_id: str, run_kwargs: dict) -> None:
    """Background worker: run the EXISTING run_tool_loop and persist the outcome.
    Never raises -- a worker exception is captured into the job record so a lost
    task can never crash the event loop."""
    rec = _job_read(job_id) or {"job_id": job_id}
    try:
        rec["status"] = "running"
        rec["started_at"] = _job_now_iso()
        _job_write(rec)
        result = await run_tool_loop(**run_kwargs)
        rec["status"] = "done"
        rec["result"] = result
        rec["finished_at"] = _job_now_iso()
        _job_write(rec)
    except Exception as e:  # noqa: BLE001 -- capture into record; never crash the loop
        rec["status"] = "failed"
        rec["error"] = f"{type(e).__name__}: {e}"
        # F-3 (2026-09-11): stamp typed error_kind for tool gate breaches so callers need not parse error text
        if isinstance(e, ToolGateBreach) or any(c.__name__ == "ToolGateBreach" for c in type(e).__mro__):
            rec["error_kind"] = "tool_gate_breach"
        rec["finished_at"] = _job_now_iso()
        try:
            _job_write(rec)
        except OSError:
            log.critical(f"build1 worker could not persist failure for job {job_id}")


def submit_job(
    *,
    run_kwargs: dict,
    role: str = "igor",
    depth: int = 0,
    tier: str = "",
) -> dict:
    """ASYNC submit: fence-check, persist a 'submitted' record, spawn a tracked
    background worker, and return {ok, job_id, status} IMMEDIATELY (well under the
    Router read-timeout ceiling). run_kwargs is the EXACT kwargs dict run_tool_loop
    takes (backend_dispatch_fn / tier / messages / gateway_call_fn / role / depth /
    ...). The synchronous path is untouched -- this is purely additive.

    Fence (reuses the B2 bound): depth >= MAX_DELEGATE_DEPTH -> refused; in-flight
    >= _MAX_JOBS_IN_FLIGHT -> refused (backpressure). A refusal returns an error
    dict and does NOT create a job or spawn a worker."""
    _reconcile_stale_jobs()
    if depth >= MAX_DELEGATE_DEPTH:
        return {
            "ok": False,
            "error": f"job refused: depth {depth} >= MAX_DELEGATE_DEPTH {MAX_DELEGATE_DEPTH}",
            "kind": "job_depth_refused",
        }
    in_flight = _jobs_in_flight()
    if in_flight >= _MAX_JOBS_IN_FLIGHT:
        return {
            "ok": False,
            "error": f"job refused: {in_flight} in-flight >= cap {_MAX_JOBS_IN_FLIGHT}",
            "kind": "job_backpressure_refused",
        }
    import uuid
    job_id = uuid.uuid4().hex[:16]
    rec = {
        "job_id": job_id,
        "status": "submitted",
        "role": role,
        "depth": depth,
        "tier": str(tier),
        "submitted_at": _job_now_iso(),
    }
    try:
        _job_write(rec)
    except OSError as e:
        return {"ok": False, "error": f"job store unwritable: {e}", "kind": "job_store_error"}
    task = asyncio.create_task(_run_job_worker(job_id, run_kwargs))
    _job_worker_tasks.add(task)
    task.add_done_callback(_job_worker_tasks.discard)
    return {"ok": True, "job_id": job_id, "status": "submitted"}


def collect_job(job_id: str) -> dict:
    """READ-ONLY poll: return {ok, status, result?/error?} for a job. Unknown id ->
    status=unknown. Never spawns work; never mutates a running/done record."""
    _reconcile_stale_jobs()
    rec = _job_read(job_id)
    if rec is None:
        return {"ok": False, "status": "unknown", "error": f"no such job {job_id}", "kind": "job_unknown"}
    out = {"ok": True, "job_id": job_id, "status": rec.get("status")}
    if rec.get("status") == "done":
        out["result"] = rec.get("result")
    elif rec.get("status") == "failed":
        out["error"] = rec.get("error")
        if rec.get("error_kind"):
            out["error_kind"] = rec.get("error_kind")
    return out


def list_jobs() -> dict:
    """READ-ONLY: summarize every job record (id/status/role/depth/tier). Bounded
    by the jobs dir; a housekeeping caller prunes done/failed records out of band."""
    _reconcile_stale_jobs()
    jobs = []
    try:
        names = sorted(os.listdir(_JOBS_DIR))
    except FileNotFoundError:
        names = []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        rec = _job_read(fn[:-5])
        if rec:
            jobs.append({k: rec.get(k) for k in ("job_id", "status", "role", "depth", "tier")})
    return {"ok": True, "jobs": jobs, "in_flight": _jobs_in_flight()}
