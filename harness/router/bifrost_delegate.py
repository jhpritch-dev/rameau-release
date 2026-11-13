r"""
bifrost_delegate.py -- BIFROST Delegate MCP Tool
=================================================
Single-tool MCP server for Claude Code. Exposes `delegate(leaf, subtask)`
so Claude Code can address distinct local BIFROST executors by Router alias,
bypassing the native Task tool hardcoded model enum.

Also exposes `list_leaves()` for capability-aware decomposition: Claude Code
calls list_leaves() first, reads the capability annotations, then assigns
scopes to leaves by fit.

Mechanism: delegate(leaf, subtask) -> POST /v1/chat/completions with
model=leaf -> leaf completes -> content returned -> Claude Code assembles.

Design decisions (2026-06-24):
  - Module-level startup: loads capability_registry.json + /v1/models once.
  - Registry miss-tolerant: aliases not present -> registry field is None.
  - Per-alias GET (/v1/models/<alias>) returns 404 -- list-and-filter only.
  - No protected file touched; bifrost_shell.py is a separate server for Igor.
  - GATE-0.5 (2026-06-24): the live capability_registry.json is an ENVELOPE
    {_comment, _updated, aliases: [ {"alias": <id>, ...}, ... ]} -- a LIST of 12
    entries, NOT a flat dict. _load_registry() flattens it to an alias-keyed dict
    so per-alias lookup works (the rev2-brief's flat _REGISTRY.get(alias) would
    have returned None for every alias). Intent (per-alias annotation,
    miss-tolerant) preserved exactly.

Machine: Bifrost (D:\Projects\bifrost-router\)
Transport: stdio (for claude mcp add)
Run: C:\Python313\python.exe D:\Projects\bifrost-router\bifrost_delegate.py
"""

import json
import os
import re
import time
from pathlib import Path

import httpx
from fastmcp import FastMCP
from shared.resolver import resolve

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# D9 (2026-10-16): env-driven. BIFROST_ROUTER_BASE is the router BASE -- this module
# appends /v1/chat/completions and /v1/models -- and is deliberately NOT the existing
# BIFROST_ROUTER_URL, which anthropic_adapter.py:29 holds as a full chat-completions
# URL. The default keeps the genericized placeholder so an unset variable fails loudly
# at connect time instead of silently reaching a plausible wrong host.
ROUTER_BASE  = resolve("router")
ROUTER_CHAT  = f"{ROUTER_BASE}/v1/chat/completions"
# Beside this module, which is where capability_registry.py:26 already resolves the same
# file. This line used to carry an absolute path from one machine -- under the comment
# block above explaining that its siblings had been made env-driven -- so the two modules
# disagreed about where one file lives, and _load_registry's `return {}` hid it.
REGISTRY_PATH = Path(__file__).parent / "capability_registry.json"

# Architect-curated safe leaf table.
# Excludes: bifrost-auto (NEVER tool-call per CLAUDE.md), n8n-ops (webhook),
# cloud tiers with base_url=None (no local-egress guarantee).
# VRAM notes (Forge 100GB GTT): Igor=65GB, gemma4=17GB, devstral=14GB, toolmaster=~20GB@16k.
# Two daytime modes (2026-07-07): CLOUD-ORCH -> fans gemma4+devstral resident, NO Igor;
# SYNTH/EVAL -> Igor resident (transient). NEVER Igor+gemma4+toolmaster (103GB = OOM).
# Max 2 concurrent Forge leaves; do not load Igor alongside any 30b-class model.
# Reliable tool leaves: granite (Bifrost), ministral (Hearth), devstral + toolmaster (Forge).
SAFE_LEAVES = {
    "bifrost-granite":         "Bifrost  | granite-4.1-8b-tekken | light-tool | tool-capable | pinned keep_alive=-1 ctx=32768 (2026-07-07)",
    "bifrost-coder":           "Bifrost  | qwen2.5-coder:7b   | mid          | NOT tool-capable (tool_calls:false)",
    "bifrost-coder-toolmaster":"Forge    | qwen3-coder:30b-a3b| heavy-coding | tool-capable | ~20GB ctx=16384",
    "bifrost-toolmaster":      "Forge    | qwen3-coder:30b-a3b| heavy-coding | REPOINTED 2026-08-12 (R-14). This NAME used to mean devstral, which failed tool use twice: 0 tool calls, tool-call JSON printed as prose, and a fabricated symbol and path. It now resolves to the SAME backend as bifrost-coder-toolmaster -- prefer that name. Requires router >= v336; on v335 the name still reaches devstral.",
    "bifrost-t2-gemma4":       "Forge    | gemma4 25.8B       | EVALUATOR    | multimodal VERDICT judge; tool ~2/3 opportunistic, NOT a reliable tool fan | 17GB",
    "bifrost-igor-solo":       "Forge    | gpt-oss-igor-65k  | heavy-reason | tool-capable | 65GB Igor (tier 2.5-solo); transient synth/eval -- do not co-load with any 30b-class leaf. (bifrost-igor alias -> tier 2.5 = qwen3-coder:30b, a dup of bifrost-coder-toolmaster; use -solo for the real Igor.)",
    "bifrost-1a-hearth":       "Hearth   | ministral-8b-tekken| light        | NOT a usable tool leaf for windowed reads. Passed a simple 3/3 tool probe 2026-07-07 (migrated off the qwen3:8b thinking-trap), but on the R-13 window task it returned ZERO tool calls in 4 of 4 attempts (2026-08-13), NARRATING the call it intended to make (\"I will first use grep_repo...\") and then answering from nothing. Same shape as the devstral failure R-14 repointed away from. Use bifrost-granite for a light tool leaf.",
}

# ---------------------------------------------------------------------------
# R-17 (2026-08-12): tier-override leaves.
#
# T_NPU_TOOLS is "addressable-ONLY" by design: X-Tier-Override: npu-tools is its reach path.
# (This line used to quote a main.py alias-pin comment calling that pin plus the header "the
# entire reach surface"; M2b, 2026-09-11, removes the pin and that comment.) The Router
# VALIDATES the model
# NAME against /v1/models, so 'bifrost-npu-tools' 400s; but the TIER override rides on any
# advertised name. Verified: model=bifrost-auto + X-Tier-Override:npu-tools -> qwen3.5:4b,
# and it carries tool_calls. No Router change, no redeploy.
#
# leaf alias -> (model name to SEND, tier override header value). EMPTY: bifrost-npu-tools was removed 2026-09-11 (M2a, not live).
# ---------------------------------------------------------------------------
_TIER_OVERRIDE_LEAVES = {
}


def _route(leaf: str):
    """(model_to_send, headers) for a leaf. Additive headers only."""
    model, tier = _TIER_OVERRIDE_LEAVES.get(leaf, (leaf, None))
    headers = {"X-Bifrost-Source": "cc_dispatch"}
    if tier:
        headers["X-Tier-Override"] = tier
    return model, headers


# ---------------------------------------------------------------------------
# Startup: load registry + live alias set (once per process, miss-tolerant)
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    """Load capability_registry.json -> dict keyed by alias id. Miss-tolerant.

    The live file is an envelope: {_comment, _updated, aliases: [ {"alias": <id>,
    "model": ..., "tool_calls": ..., "ctx_len": ..., "profile_tags": [...], ...},
    ... ]}. We flatten the `aliases` LIST into {alias_id: entry}. Returns {} on
    any error so a registry miss or malformed file never crashes startup.
    """
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        entries = data.get("aliases", [])
        if isinstance(entries, list):
            return {e["alias"]: e for e in entries
                    if isinstance(e, dict) and "alias" in e}
        if isinstance(entries, dict):
            return entries
        return {}
    except Exception as exc:
        print(f"[bifrost-delegate] registry load failed: {exc}", flush=True)
        return {}


def _load_live_aliases() -> set:
    """Fetch /v1/models list, return set of alias ids. Returns empty set on error."""
    try:
        r = httpx.get(f"{ROUTER_BASE}/v1/models", timeout=10)
        return {m["id"] for m in r.json().get("data", [])}
    except Exception as exc:
        print(f"[bifrost-delegate] /v1/models fetch failed: {exc}", flush=True)
        return set()


_REGISTRY    = _load_registry()
_LIVE_ALIASES = _load_live_aliases()

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="bifrost-delegate",
    instructions=(
        "Dispatch subtasks to BIFROST leaf executors by Router alias. "
        "Workflow: (1) call list_leaves() to read capability annotations, "
        "(2) assign scopes to leaves by fit, "
        "(3) call delegate() for each leaf in parallel or sequence, "
        "(4) check ok=True on each result before assembling. "
        "VRAM rule: max 2 concurrent Forge leaves. "
        "Do not co-load bifrost-igor-solo (the 65GB Igor) with any 30b-class leaf."
    ),
)


# ---------------------------------------------------------------------------
# Tool: list_leaves
# ---------------------------------------------------------------------------

@mcp.tool()
def list_leaves(verbose: bool = False) -> dict:
    """
    Return the safe leaf alias table with capability annotations.

    Each entry is annotated from the capability registry where available
    (tool_calls, ctx_len, profile_tags). Aliases not in the registry
    are returned with SAFE_LEAVES curator notes only.

    Call this before decomposing a task so leaf assignment is capability-aware.

    verbose=False (default): per leaf only note (<=160 chars), in_live_router, tool_calls,
    ctx_len and up to 4 suited_for tags -- small enough for a local model's context.
    verbose=True: the full registry entry per leaf (the pre-2026-09-10 shape, ~16 KB).

    Returns:
        {
            "leaves": {
                "<alias>": {
                    "curator_note": str,
                    "in_live_router": bool,
                    "registry": dict | None,
                }
            },
            "vram_guidance": str,
            "registry_coverage": str,
        }
    """
    # E4 2026-09-10: compact by default. The full shape (every registry field for every leaf) is
    # ~16 KB, and a bifrost-t2-gemma4 opencode session that called this overflowed its provider
    # size limit and exited 1. verbose=True returns the original shape, unchanged.
    leaves = {}
    for alias, note in SAFE_LEAVES.items():
        if verbose:
            leaves[alias] = {
                "curator_note":   note,
                "in_live_router": alias in _LIVE_ALIASES,
                "registry":       _REGISTRY.get(alias),   # None if not covered
            }
            continue
        reg = _REGISTRY.get(alias) or {}
        leaves[alias] = {
            "note":           note if len(note) <= 160 else note[:157] + "...",
            "in_live_router": alias in _LIVE_ALIASES,
            "tool_calls":     reg.get("tool_calls"),
            "ctx_len":        reg.get("ctx_len"),
            "suited_for":     (reg.get("suited_for") or [])[:4],
        }

    covered = sum(1 for a in SAFE_LEAVES if a in _REGISTRY)
    return {
        "leaves": leaves,
        "vram_guidance": (
            "Forge 100GB GTT. Two modes: CLOUD-ORCH = fans gemma4(17GB)+devstral(14GB) resident, NO Igor; "
            "SYNTH/EVAL = Igor(65GB) resident transient. NEVER co-load Igor+gemma4+toolmaster (103GB=OOM). "
            "Max 2 concurrent Forge leaves; bifrost-igor-solo (65GB Igor) must not co-load with any 30b-class model. "
            "OLLAMA_MAX_LOADED_MODELS=3, NUM_PARALLEL=1 (Forge)."
        ),
        "registry_coverage": f"{covered}/{len(SAFE_LEAVES)} leaves have registry annotations",
    }


# ---------------------------------------------------------------------------
# Read-only tool loop (R-08, 2026-08-12)
#
# WHY HERE AND NOT IN THE ROUTER: this module is a stdio MCP server that runs LOCALLY ON
# BIFROST, where both repos live -- including the working tree, which the Router pod's baked
# /app can never show. Putting the loop here touches no protected file (igor_tool_loop.py and
# policy_instructions.py are rule-1 protected and are NOT involved), needs no deploy, and
# leaves the Router a pure completion backend.
#
# READ-ONLY BY CONSTRUCTION. No write tool, no shell tool, no network tool. Adding one is a
# design change, not a config tweak -- the moment a write lands here this becomes a remote
# execution surface on the machine holding the repos.
# ---------------------------------------------------------------------------

# The roots this read-only surface may reach. ENV-DRIVEN, AND THE DEFAULT IS THIS MODULE'S
# OWN DIRECTORY -- not a wider guess. These used to be two absolute paths from one machine,
# one of them carrying an account name.
#
# THE DEFAULT IS DELIBERATELY NARROW rather than absent. An empty root list would make the
# allowlist vacuous, and a vacuous allowlist on a read-only tool surface fails OPEN in the
# sense that matters: every path check below compares against this list, so getting it
# wrong is a containment failure and not a missing feature. One real directory is the
# smallest value that keeps the check meaningful.
_TOOL_ROOTS = [
    Path(p) for p in os.environ.get("BIFROST_TOOL_ROOTS", "").split(os.pathsep) if p.strip()
] or [Path(__file__).parent]

# Denied even inside an allowed root.
_TOOL_DENY = re.compile(
    r"(^|[\\/])(\.git|__pycache__|\.venv|node_modules)([\\/]|$)"
    r"|(^|[\\/])\.env"
    r"|\.(pem|key|pfx|p12)$"
    r"|(^|[\\/])id_(rsa|ed25519|ecdsa)"
    r"|secret|token|credential",
    re.I,
)

_MAX_ITERS       = 6
_MAX_READ_BYTES  = 64 * 1024
_MAX_TOTAL_BYTES = 256 * 1024
_MAX_HITS        = 100
_MAX_ENTRIES     = 200
_MAX_WINDOW_LINES = 400   # R-13 2026-08-12: cap on a read_file line window (LARGEST allowed)

# R-19 (2026-08-12): the window must fit the TARGET LEAF, not the biggest leaf.
#
# A 400-line window is ~20,900 B (~5-6k tokens). Tier npu-tools is num_ctx 8192 -- and its own
# fleet_config note says that figure is "NOT live-verified". Add the R-16 preamble and the
# injected tool schemas and ONE windowed read fills the context, which is exactly what was
# observed: repeated read_file calls, budget exhausted, no answer.
#
# ctx_len is read from capability_registry.json where present. Leaves whose context lives only
# in fleet_config (tier-override leaves have no registry entry) are listed explicitly. EMPTY since M2a 2026-09-11.
_LEAF_CTX_OVERRIDE = {
}

_WINDOW_CTX_FRACTION = 0.20   # share of the leaf's context a single tool result may occupy
_CHARS_PER_TOKEN     = 3.5    # conservative for source code
_CHARS_PER_LINE      = 60     # conservative for source code
_MIN_WINDOW_LINES    = 20     # below this a window is useless; prefer grep_repo


def _window_budget(leaf: str):
    """(max_lines, max_bytes) for one tool result sent to `leaf`.

    FAIL-OPEN: an unknown leaf gets today's defaults. Absence of a ctx_len is absence of
    knowledge, not evidence of a small context -- narrowing on a guess would silently degrade
    a large leaf.
    """
    ctx = _LEAF_CTX_OVERRIDE.get(leaf)
    if ctx is None:
        reg = _REGISTRY.get(leaf) or {}
        ctx = reg.get("ctx_len")
    if not ctx:
        return _MAX_WINDOW_LINES, _MAX_READ_BYTES
    budget_chars = int(ctx * _WINDOW_CTX_FRACTION * _CHARS_PER_TOKEN)
    lines = max(_MIN_WINDOW_LINES, min(_MAX_WINDOW_LINES, budget_chars // _CHARS_PER_LINE))
    nbytes = max(2048, min(_MAX_READ_BYTES, budget_chars))
    return lines, nbytes

# Secret shapes redacted on egress. Tool output is UNTRUSTED DATA: it is inserted as
# role:"tool" content and must never be treated as instructions.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|-----BEGIN[^\n]*-----|[A-Fa-f0-9]{40,})"
)


def _redact(text: str) -> str:
    def sub(m):
        v = m.group(0)
        return "[REDACTED len=%d prefix=%s]" % (len(v), v[:4])
    return _SECRET_RE.sub(sub, text)


def _safe_path(raw: str):
    """Resolve FIRST, then check containment. That order is what stops .. traversal."""
    try:
        p = Path(raw).resolve()
    except Exception as exc:
        return None, "bad path: %s" % exc
    if not any(str(p).lower().startswith(str(r.resolve()).lower()) for r in _TOOL_ROOTS):
        return None, ("REFUSED: %s is outside the allowed roots %s"
                      % (p, [str(r) for r in _TOOL_ROOTS]))
    if _TOOL_DENY.search(str(p)):
        return None, "REFUSED: %s matches the deny-list (vcs/secret/venv)" % p
    return p, ""


def _t_list_dir(path: str, depth: int = 1) -> str:
    p, err = _safe_path(path)
    if err:
        return err
    if not p.exists():
        return "no such path: %s" % p
    depth = max(1, min(int(depth or 1), 2))
    out, n = [], 0
    for entry in sorted(p.rglob("*") if depth > 1 else p.glob("*")):
        if _TOOL_DENY.search(str(entry)):
            continue
        out.append("%s%s  %d" % (entry.name, "/" if entry.is_dir() else "",
                                 entry.stat().st_size if entry.is_file() else 0))
        n += 1
        if n >= _MAX_ENTRIES:
            out.append("... truncated at %d entries" % _MAX_ENTRIES)
            break
    return "\n".join(out) if out else "(empty)"


def _t_read_file(path: str, max_bytes: int = _MAX_READ_BYTES,
                 start_line: int = 0, end_line: int = 0,
                 _win_lines: int = _MAX_WINDOW_LINES,
                 _win_bytes: int = _MAX_READ_BYTES) -> str:
    """Read a file. Without a window this returns bytes 0..64KB, as it always has.

    R-13 (2026-08-12): before this, EVERY read started at byte 0 and stopped at
    _MAX_READ_BYTES, so ~73% of autopilot_graph.py (241 KB) was unreachable by any
    argument a leaf could supply. Leaves burned their whole 6-call budget re-reading
    the same opening bytes with a different cap. start_line/end_line composes with
    what grep_repo already returns (file:line), which is the only navigation the
    toolset offers.
    """
    p, err = _safe_path(path)
    if err:
        return err
    if not p.is_file():
        return "not a file: %s" % p

    # R-21 (2026-08-12): distinguish ABSENT from MALFORMED. Before this, start_line="" was
    # FALSY and fell through to the legacy branch -- a 64 KB slab into an 8k-context leaf,
    # bypassing the R-19 budget entirely. An omitted bound still means "legacy"; a bound that
    # is PRESENT but empty or non-integer is now a corrective error naming the required shape,
    # because a model cannot act on "ValueError: invalid literal for int()".
    _shape = ('{"path": "<file>", "start_line": 3479, "end_line": 3510}')
    for _k, _v in (("start_line", start_line), ("end_line", end_line)):
        if isinstance(_v, str) and not _v.strip():
            return ('tool error: "%s" was empty. OMIT it to read from the start of the file, '
                    'or pass an INTEGER. start_line and end_line are SEPARATE integer fields: '
                    '%s' % (_k, _shape))
        if _v is not None and _v != 0 and _v != "":
            try:
                int(str(_v).strip())
            except (TypeError, ValueError):
                return ('tool error: "%s" must be a single INTEGER, got %r. Do not put two '
                        'numbers, or another argument, in one field. Correct form: %s'
                        % (_k, _v, _shape))
    start_line = int(str(start_line).strip()) if str(start_line or "").strip() else 0
    end_line = int(str(end_line).strip()) if str(end_line or "").strip() else 0

    # LEGACY PATH -- byte-identical to pre-R-13 when no window is requested.
    if not start_line and not end_line:
        cap = max(1, min(int(max_bytes or _MAX_READ_BYTES), _MAX_READ_BYTES))
        # R-21: the legacy branch must respect the leaf budget too. _win_bytes defaults to
        # _MAX_READ_BYTES, so an UNBUDGETED call is unchanged; a budgeted one can only narrow.
        cap = min(cap, max(256, min(int(_win_bytes or _MAX_READ_BYTES), _MAX_READ_BYTES)))
        raw = p.read_bytes()[:cap]
        text = raw.decode("utf-8", errors="replace")
        if p.stat().st_size > cap:
            text += "\n... TRUNCATED at %d of %d bytes" % (cap, p.stat().st_size)
        return _redact(text)

    # WINDOW PATH. Every bound clamps; nothing here raises on bad input.
    lines = p.read_bytes().decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    s = max(1, int(start_line or 1))
    if s > total:
        return "start_line %d is past end of file (%d lines)" % (s, total)
    # R-19: _win_lines/_win_bytes default to the global caps, so an unbudgeted call is
    # byte-identical to R-13. A caller-supplied budget can only NARROW them.
    win_lines = max(1, min(int(_win_lines or _MAX_WINDOW_LINES), _MAX_WINDOW_LINES))
    win_bytes = max(256, min(int(_win_bytes or _MAX_READ_BYTES), _MAX_READ_BYTES))
    e = int(end_line or 0) or (s + win_lines - 1)
    e = max(s, min(e, total, s + win_lines - 1))
    body = "\n".join("%d: %s" % (n, lines[n - 1]) for n in range(s, e + 1))
    out = "lines %d-%d of %d in %s\n" % (s, e, total, p.name) + body
    # R-21: reserve room for the note. This used to slice to win_bytes and THEN append,
    # so the result exceeded the very budget it had just enforced.
    _note = "\n... TRUNCATED at %d bytes" % win_bytes
    _enc = out.encode("utf-8")
    if len(_enc) > win_bytes:
        _keep = max(0, win_bytes - len(_note.encode("utf-8")))
        out = _enc[:_keep].decode("utf-8", errors="ignore") + _note
    return _redact(out)


def _t_grep_repo(pattern: str, path: str, glob: str = "*.py", max_hits: int = _MAX_HITS) -> str:
    p, err = _safe_path(path)
    if err:
        return err
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return "bad regex: %s" % exc
    cap, hits = max(1, min(int(max_hits or _MAX_HITS), _MAX_HITS)), []
    for f in sorted(p.rglob(glob or "*.py")):
        if _TOOL_DENY.search(str(f)) or not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append("%s:%d: %s" % (f, i, line.strip()[:200]))
                    if len(hits) >= cap:
                        return _redact("\n".join(hits) + "\n... truncated at %d hits" % cap)
        except Exception:
            continue
    return _redact("\n".join(hits)) if hits else "no matches"


_TOOL_IMPL = {"list_dir": _t_list_dir, "read_file": _t_read_file, "grep_repo": _t_grep_repo}

_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "List a directory inside the BIFROST repos (read-only).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "depth": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": ("Read a text file inside the BIFROST repos (read-only). WITHOUT "
                        "start_line this returns only the FIRST 64 KB of the file. To read "
                        "anywhere else, pass start_line/end_line -- use this after grep_repo "
                        "gives you a line number."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "max_bytes": {"type": "integer"},
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep_repo", "description": "Regex-search files inside the BIFROST repos (read-only).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
            "glob": {"type": "string"}, "max_hits": {"type": "integer"}}, "required": ["pattern", "path"]}}},
]


def _dispatch_tool(name: str, args_json: str, budget=None) -> str:
    """Fail-closed: an unknown tool is refused, never dispatched.

    R-19: `budget` is (max_lines, max_bytes) for the calling leaf. It is injected here, AFTER
    the model's own arguments are parsed, and the private names are stripped from those
    arguments first -- a leaf must not be able to widen its own window.
    """
    import json as _json
    fn = _TOOL_IMPL.get(name)
    if fn is None:
        return "REFUSED: unknown tool %r (allowed: %s)" % (name, sorted(_TOOL_IMPL))
    try:
        kwargs = _json.loads(args_json) if isinstance(args_json, str) else dict(args_json or {})
    except Exception as exc:
        return "REFUSED: unparseable arguments (%s)" % exc
    if not isinstance(kwargs, dict):
        return "REFUSED: arguments must be an object, got %s" % type(kwargs).__name__
    for _private in ("_win_lines", "_win_bytes"):
        kwargs.pop(_private, None)
    if budget and name == "read_file":
        kwargs["_win_lines"], kwargs["_win_bytes"] = budget
    try:
        _out = fn(**kwargs)
        # R-21: a SINGLE choke point for the byte budget. Each branch of _t_read_file used to
        # slice to the cap and THEN append a "TRUNCATED" note, so it exceeded the very cap it
        # had just enforced (measured: legacy 5772 B and window 5781 B against a 5734 B cap).
        # Fixing branches one at a time is exactly how that survived R-19; clamp once, here,
        # so no present or future branch can overrun a leaf's context.
        if budget and isinstance(_out, str):
            _cap = int(budget[1] or 0)
            if _cap and len(_out.encode("utf-8")) > _cap:
                _out = _out.encode("utf-8")[:_cap].decode("utf-8", errors="ignore")
        return _out
    except TypeError as exc:
        return "REFUSED: bad arguments for %s (%s)" % (name, exc)
    except Exception as exc:
        return "tool error: %s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Tool: delegate
# ---------------------------------------------------------------------------

def _extract_router_error(exc: Exception):
    """F-4 (2026-09-11): extract (status_code, parsed_error_object) from HTTP error.
    Retains Router's structured error object (bounded) instead of discarding raw body."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.json()
            if isinstance(body, dict) and "error" in body:
                raw_err = body["error"]
            elif isinstance(body, dict):
                raw_err = body
            else:
                raw_err = {"message": str(body)[:300]}
            if isinstance(raw_err, dict):
                bounded = {}
                for k, v in raw_err.items():
                    if isinstance(v, str):
                        bounded[k] = v[:500]
                    elif isinstance(v, (int, float, bool)) or v is None:
                        bounded[k] = v
                    elif isinstance(v, dict):
                        bounded[k] = {sk: (sv[:500] if isinstance(sv, str) else sv) for sk, sv in v.items()}
                    else:
                        bounded[k] = str(v)[:500]
                return status, bounded
            return status, {"message": str(raw_err)[:500]}
        except Exception:
            return status, {"message": exc.response.text[:300]}
    return None, f"{type(exc).__name__}: {exc}"


@mcp.tool()
async def delegate(
    leaf: str,
    subtask: str,
    timeout: int = 120,
    tools: bool = False,
) -> dict:
    """
    Dispatch a subtask to a named BIFROST leaf executor via the Router.

    Args:
        leaf:    Router alias. It must be in SAFE_LEAVES -- anything else is refused before any
                 network call. Call list_leaves() to see them; that list is the only
                 source of truth (this docstring once named bifrost-igor, which is not
                 in SAFE_LEAVES).
        subtask: The prompt or task to deliver to the leaf.
        timeout: Request timeout in seconds. Default 120. Use 240 for Igor.

    Returns:
        {
            "ok":        bool,    # True = leaf responded with content
            "leaf":      str,
            "content":   str,     # completion text (empty on error)
            "elapsed_s": float,
            "usage":     dict,    # token counts (may be {} for local models)
            "registry":  dict,    # capability entry if covered, else None
            "error":     str,     # populated only when ok=False
        }

    Never raises. A failed leaf returns ok=False with error populated.
    Surface the error; do not abort the orchestration.
    """
    # SAFE_LEAVES enforcement (2026-09-11). SAFE_LEAVES is the architect-curated allowlist and
    # list_leaves() advertises exactly it, but nothing here consulted it: any name the live
    # Router advertises (bifrost-auto, cloud tiers, claude-*) was dispatchable, and when the
    # /v1/models fetch had failed ANY string was POSTed. Refuse before any network call.
    # Exact match with no case/whitespace folding -- the same comparison _route(), _REGISTRY
    # and _LIVE_ALIASES use. A tier-override leaf must be listed here too (bifrost-npu-tools
    # was, until M2a).
    if not isinstance(leaf, str) or leaf not in SAFE_LEAVES:
        return {
            "ok": False,
            "leaf": leaf,
            "content": "",
            "elapsed_s": 0.0,
            "usage": {},
            "registry": _REGISTRY.get(leaf) if isinstance(leaf, str) else None,
            "error": (
                f"leaf {leaf!r} is not in SAFE_LEAVES; refusing to dispatch. Call list_leaves() "
                f"and pick one of: {sorted(SAFE_LEAVES)}"
            ),
        }

    # Pre-flight: refuse an alias the live Router cannot route, rather than letting it
    # surface as a bare `400 Bad Request` from /v1/chat/completions. list_leaves() already
    # reports in_live_router; delegate() used to ignore it. An alias can be registered in
    # capability_registry + SAFE_LEAVES yet be absent from the deployed Router, because the
    # pod bakes fleet_config at build time (e.g. bifrost-coder-tool, until M2a 2026-09-11 removed
    # it from SAFE_LEAVES).
    #
    # FAIL-OPEN when we do not know: _load_live_aliases() returns an EMPTY set if the
    # /v1/models fetch failed, and an empty set must not mean "nothing is routable". Absence
    # of knowledge is not evidence of absence -- so only refuse when we positively have a
    # live alias list that excludes this leaf.
    # Tier-override leaves are INTENTIONALLY absent from /v1/models -- absence is the design,
    # not a missing deploy. Exempt them, or the guard below refuses a leaf that works.
    if _LIVE_ALIASES and leaf not in _LIVE_ALIASES and leaf not in _TIER_OVERRIDE_LEAVES:
        return {
            "ok": False,
            "leaf": leaf,
            "content": "",
            "elapsed_s": 0.0,
            "usage": {},
            "registry": _REGISTRY.get(leaf),
            "error": (
                f"leaf '{leaf}' is not routable by the live Router (absent from /v1/models). "
                f"It may be registered but not yet deployed -- the Router pod bakes fleet_config "
                f"at build time. Live aliases: {sorted(_LIVE_ALIASES)}"
            ),
        }

    # tools=True requires a tool-capable leaf. Refuse loudly rather than silently degrading
    # to a plain completion the caller would mistake for a tool-backed answer.
    if tools:
        reg = _REGISTRY.get(leaf) or {}
        if reg and not reg.get("tool_calls"):
            return {
                "ok": False, "leaf": leaf, "content": "", "elapsed_s": 0.0, "usage": {},
                "registry": reg, "tools_called": [],
                "error": f"leaf '{leaf}' is tool_calls:false; refusing tools=True",
            }
        return await _delegate_with_tools(leaf, subtask, timeout)

    _model, _headers = _route(leaf)
    payload = {
        "model":    _model,
        "messages": [{"role": "user", "content": subtask}],
        "stream":   False,
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            # Caller-side attribution: tag the dispatch leg so the Router
            # (main.py:1419) records source=cc_dispatch in InferenceEvent/Langfuse
            # instead of leaving the trace unattributed. Additive header only.
            r = await client.post(
                ROUTER_CHAT, json=payload,
                headers=_headers,
            )
            r.raise_for_status()
            data = r.json()

        elapsed = round(time.monotonic() - t0, 2)
        return {
            "ok":        True,
            "leaf":      leaf,
            "content":   data["choices"][0]["message"]["content"],
            "elapsed_s": elapsed,
            "usage":     data.get("usage", {}),
            "registry":  _REGISTRY.get(leaf),
            "error":     "",
        }

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 2)
        status, err_obj = _extract_router_error(exc)
        out = {
            "ok":        False,
            "leaf":      leaf,
            "content":   "",
            "elapsed_s": elapsed,
            "usage":     {},
            "registry":  _REGISTRY.get(leaf),
            "error":     err_obj,
        }
        if status is not None:
            out["status"] = status
        return out


# R-16 (2026-08-12): leaves execute a SHOWN call and fail to synthesize one.
# Measured: qwen3.5:4b on the NPU passed a window task in ONE call when given the example
# below, matching qwen3-coder:30b-a3b. Without it, a 9b grepped, received the answer, and
# returned {"line": 0}. State the tools' real limits and show one concrete call.
_TOOL_PREAMBLE = (
    "You have read-only tools over the BIFROST repos. Use them; do not answer from memory.\n"
    "\n"
    "HOW THE TOOLS ACTUALLY BEHAVE:\n"
    "- grep_repo returns lines already formatted as \"file:line: text\". It is the right tool\n"
    "  for \"where is X\" and \"does X exist\". Search the EXACT symbol, including any leading\n"
    "  underscore. It returns single lines and CANNOT show a function body.\n"
    "- read_file WITHOUT start_line returns only the FIRST 64 KB of the file. Large files are\n"
    "  mostly unreachable that way. To read anywhere else, pass start_line/end_line, e.g.:\n"
    "      read_file(path=\"D:\\\\Projects\\\\bifrost-router\\\\autopilot_graph.py\",\n"
    "                start_line=3470, end_line=3490)\n"
    "  Typical order: grep_repo to find the line number, then read_file with a window around it.\n"
    "- list_dir lists a directory. It does not search; prefer grep_repo for finding things.\n"
    "\n"
    "RULES:\n"
    "- An EMPTY grep result is NOT proof of absence. It is far more often the wrong pattern or\n"
    "  the wrong path. Vary the pattern once before concluding something does not exist.\n"
    "- When a tool result already answers the question, STOP calling tools and reply. Repeating\n"
    "  an identical call returns identical bytes and wastes the budget.\n"
    "- Report what you actually READ, including real line numbers. Never invent a path or a\n"
    "  line number; if you could not reach something, say so plainly.\n"
)


async def _delegate_with_tools(leaf: str, subtask: str, timeout: int) -> dict:
    """Read-only tool loop. Returns the same shape as delegate() plus tools_called.

    tools_called is the TRUST BOUNDARY, not telemetry: without it a leaf that hallucinated a
    file's contents is indistinguishable from one that read it. Trust a read-only claim iff
    the tool appears here, then spot-verify substance.
    """
    _budget = _window_budget(leaf)   # R-19: (max_lines, max_bytes) sized to THIS leaf
    msgs = [{"role": "system", "content": _TOOL_PREAMBLE},
            {"role": "user", "content": subtask}]
    called, total_bytes = [], 0
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            for _ in range(_MAX_ITERS):
                _model, _headers = _route(leaf)
                r = await client.post(
                    ROUTER_CHAT,
                    json={"model": _model, "messages": msgs, "stream": False,
                          "tools": _TOOL_SCHEMAS},
                    headers=_headers,
                )
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    return {
                        "ok": True, "leaf": leaf, "content": msg.get("content") or "",
                        "elapsed_s": round(time.monotonic() - t0, 2),
                        "usage": r.json().get("usage", {}), "registry": _REGISTRY.get(leaf),
                        "tools_called": called, "error": "",
                    }
                msgs.append(msg)
                for c in tcs:
                    fn = c.get("function", {})
                    out = _dispatch_tool(fn.get("name", ""), fn.get("arguments", "{}"),
                                         budget=_budget)
                    total_bytes += len(out)
                    called.append({"tool": fn.get("name", ""),
                                   "args": str(fn.get("arguments", ""))[:200],
                                   "bytes": len(out),
                                   "refused": out.startswith("REFUSED")})
                    msgs.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                 "content": out[:_budget[1]]})
                if total_bytes > _MAX_TOTAL_BYTES:
                    break
        return {
            "ok": True, "leaf": leaf,
            "content": "[tool budget exhausted after %d call(s)]" % len(called),
            "elapsed_s": round(time.monotonic() - t0, 2), "usage": {},
            "registry": _REGISTRY.get(leaf), "tools_called": called,
            "error": "budget exhausted (iters/bytes)",
        }
    except Exception as exc:
        status, err_obj = _extract_router_error(exc)
        out = {
            "ok": False, "leaf": leaf, "content": "",
            "elapsed_s": round(time.monotonic() - t0, 2), "usage": {},
            "registry": _REGISTRY.get(leaf), "tools_called": called,
            "error": err_obj,
        }
        if status is not None:
            out["status"] = status
        return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
