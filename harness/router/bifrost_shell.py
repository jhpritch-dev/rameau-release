"""
bifrost_shell.py — BIFROST Shell MCP Tool Server
=================================================
Exposes static analysis tools as MCP-callable functions.
Used by Verification Harness Level 3 in AUTOPILOT subtask pipeline.

Tools:
  run_ruff   — lint Python code, return violations
  run_mypy   — type-check Python code, return errors
  run_checks — run both ruff + mypy, return combined pass/fail

Usage:
  # Start server (stdio transport for LangGraph MCP adapter):
  python bifrost_shell.py

  # Or HTTP transport for direct REST calls:
  python bifrost_shell.py --transport http --port 8086

Machine: Bifrost (D:\\Projects\\bifrost-router\\)
"""

import subprocess
import tempfile
import os
import json
import asyncio
import functools
import logging
import time
import uuid
from pathlib import Path

from fastmcp import FastMCP

# Shared static-analysis implementations (single source of truth, see checks.py).
# Imported as plain functions and re-exposed below through this server's own
# @mcp.tool()/@gated_tool wrappers so MCP registration and the ANALYZE policy
# gate are preserved.
from checks import (
    run_ruff as _run_ruff,
    run_mypy as _run_mypy,
    run_checks as _run_checks,
)

mcp = FastMCP(
    name="bifrost-shell",
    instructions=(
        "Static analysis tools for BIFROST code verification. "
        "Use run_checks() to validate generated Python code before delivery. "
        "Returns structured pass/fail results with line-level error details."
    ),
)

# ---------------------------------------------------------------------------
# Policy gate (Docker-MCP-2 A): every @mcp.tool wraps through @gated_tool.
# Policy classes (READ/ANALYZE/WRITE/EXECUTE) live in bifrost_shell_policy.json.
# A: READ + ANALYZE enabled; WRITE + EXECUTE denied (present in code, policy-denied).
# B: flip policy flags + fill enforcement bodies (write allowlist, EXECUTE jail).
# Pattern: Session O coevolution Policy layer applied to MCP tool execution.
# ---------------------------------------------------------------------------

_POLICY_PATH = Path(__file__).parent / "bifrost_shell_policy.json"
_POLICY = json.loads(_POLICY_PATH.read_text(encoding="utf-8")) if _POLICY_PATH.exists() else {"classes": {}, "tools": {}}
_audit = logging.getLogger("bifrost_shell.gate")

# --- Ceremony 3 (agtx Adoption C), 2026-09-21 ------------------------------
# bifrost-gates.toml is a SECOND, ADDITIVE gate over bifrost_shell_policy.json.
# It is consulted only after _POLICY has already allowed the class, so it can
# turn an allow into a deny and nothing else -- it cannot grant permission
# _POLICY withheld. _POLICY remains the authority.
#
# The import is guarded because this module is the MCP tool surface: a missing
# file, an unparsable one, or an interpreter without tomllib must degrade to
# "no gates", never to an ImportError that takes every tool down.
# The halt switch is imported SEPARATELY from the gates, and first. A broken
# gates module must not be able to take the kill switch down with it -- the one
# control you reach for when things are going wrong is the one that must not
# share a failure mode with the thing that is wrong.
try:
    import bifrost_halt as _bifrost_halt
except Exception as _halt_exc:                      # noqa: BLE001
    _bifrost_halt = None
    _audit.warning("halt switch unavailable: %s", _halt_exc)

try:
    import bifrost_gates as _bifrost_gates
    _GATES = _bifrost_gates.GateStore(
        Path(__file__).parent / "bifrost-gates.toml",
        overlay_path=os.getenv("BIFROST_GATES_OVERLAY") or None,
    )
except Exception as _gates_exc:                     # noqa: BLE001 - see above
    _bifrost_gates = None
    _GATES = None
    _audit.warning("gates unavailable, policy alone applies: %s", _gates_exc)


def _deny_reason(permission_class: str):
    """Which control denies this class: "halt", "gates", or None.

    Ceremony 6 split this out of _gate_denies. The ORDER and the fail-open
    behavior are unchanged; the only new thing is that the caller learns WHICH
    control fired, so a denial can say so instead of always blaming the policy.

    No return annotation on purpose: this module imports no typing names, and a
    ceremony is the wrong place to add an import nothing else needs.

    Fails open to the existing behavior on purpose. Because _POLICY has
    already had its say before this is called, failing open here can never
    grant more than _POLICY granted -- it can only decline to tighten.
    """
    # The halt switch is checked FIRST and independently of the gates. It parses
    # nothing, so there is no syntax to get wrong at the moment it matters, and
    # it still works when the gates file is missing or unparsable.
    if _bifrost_halt is not None:
        try:
            if _bifrost_halt.halted():
                _audit.warning("HALTED, denying %s: %s", permission_class,
                               _bifrost_halt.reason())
                return "halt"
        except Exception:                           # noqa: BLE001
            pass

    if _GATES is None:
        return None
    try:
        gate = _GATES.tool_class(permission_class)
    except Exception:                               # noqa: BLE001
        return None
    return "gates" if gate.get("enabled") is False else None


def _gate_denies(permission_class: str) -> bool:
    """Preserved boolean shape for any caller that does not need the reason."""
    return _deny_reason(permission_class) is not None


# Ceremony 6: one message per CAUSE. All three printed "denied by policy", which
# sends a reader to edit bifrost_shell_policy.json -- a protected file -- to fix
# something that is not there. The halt case most needed naming and was the most
# thoroughly hidden.
_DENY_TEXT = {
    "policy": "denied by policy: {cls} class disabled in bifrost_shell_policy.json",
    "gates": "denied by gates: {cls} class disabled in bifrost-gates.toml (or its "
             "overlay). The policy still allows it; a deployment tightened it.",
    "halt": "denied by HALT: all tool calls are stopped by the operator kill switch "
            "(bifrost_halt). Neither the policy nor the gates changed. Clear the "
            "halt to restore; do not edit the policy.",
}


def _policy_denied(fn_name: str, permission_class: str, cause: str = "policy") -> dict:
    _audit.warning("DENIED tool=%s class=%s cause=%s", fn_name, permission_class, cause)
    return {
        "error": "Tool '%s' %s" % (
            fn_name,
            _DENY_TEXT.get(cause, _DENY_TEXT["policy"]).format(cls=permission_class)),
        "tool": fn_name,
        "permission_class": permission_class,
        # Ceremony 6 adds denied_by. policy_denied is DELIBERATELY unchanged: it is
        # the field callers branch on and that verify_gates_in_image.py asserts.
        # Renaming it would be tidier and would break the verifier that proves the
        # kill switch works. Not worth it.
        "denied_by": cause,
        "policy_denied": True,
    }


def gated_tool(permission_class: str):
    """Wrap a tool body with a policy check. Present-but-denied tools return a structured
    error instead of executing. B enables tools by flipping policy + filling per-class
    enforcement (write allowlist, subprocess jail); this chokepoint stays unchanged."""
    def decorator(fn):
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                cls = _POLICY.get("classes", {}).get(permission_class, {})
                if not cls.get("enabled", False):
                    return _policy_denied(fn.__name__, permission_class, "policy")
                _why = _deny_reason(permission_class)
                if _why:
                    return _policy_denied(fn.__name__, permission_class, _why)
                return await fn(*args, **kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            cls = _POLICY.get("classes", {}).get(permission_class, {})
            if not cls.get("enabled", False):
                return _policy_denied(fn.__name__, permission_class, "policy")
            _why = _deny_reason(permission_class)
            if _why:
                return _policy_denied(fn.__name__, permission_class, _why)
            return fn(*args, **kwargs)
        return sync_wrapper
    return decorator


# ---------------------------------------------------------------------------
# Stack B (2026-06-19): read-only repo INVESTIGATE tools (grep_repo/list_dir/git_read).
# CRITICAL secret-safety -- these feed a model's context, so a leaked secret is BANNED:
#   (1) path-scope: every access resolved + MUST stay under an allow-listed repo root;
#   (2) secret-path deny: files/dirs matching deny patterns are NEVER read/listed;
#   (3) content redaction: secret-looking VALUES in returned text -> [REDACTED-SECRET];
#   (4) result caps; (5) read-only BY CONSTRUCTION (grep = os.walk+re, never shell; git =
#       subprocess list-form, log/show/diff ONLY, never a mutating verb).
# Repos are RO-mounted at /repos/* by the gateway catalog (bifrost-local.yaml). Absent the
# mount the tools return not-found (inert/safe).
# ---------------------------------------------------------------------------
import re as _re_sb
import fnmatch as _fnmatch_sb

_SB_REPO_ROOTS = {
    "bifrost-router": Path("/repos/bifrost-router"),
    "autoagent": Path("/repos/autoagent"),
    # Ceremony 17 (2026-09-22): the coordination corpus, mounted read-only. This is the
    # membership PACKET-73 is about -- every host-FS tool (read_file, grep_repo,
    # list_dir, git_read) resolves through _sb_resolve against THIS dict, so adding a
    # root is what actually grants reach, not the docker -v line.
    #
    # The corpus is a snapshot of the WORKING TREE only; .git is never shipped, because
    # the inhouse repo has credentials in its history. _SB_DIR_DENY, _sb_name_is_secret
    # and _sb_redact all still apply on top of that, unchanged.
    "coordination": Path("/repos/coordination"),
}
_SB_SECRET_NAME_DENY = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.crt", "*.p12", "*.pfx",
    "*.keystore", "*.jks", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "*secret*", "*token*", "*credential*", "*password*", ".git-credentials",
    ".npmrc", ".netrc", ".pypirc", "*.ovpn",
]
_SB_DIR_DENY = {".git", ".ssh", ".gnupg", ".aws", "secrets", "node_modules",
                "__pycache__", ".venv", "venv", ".kilo", ".claude"}
# Perf-only walk skips. NOT a security control and deliberately NOT in _SB_DIR_DENY:
# _sb_resolve() rejects every _SB_DIR_DENY segment, so putting "logs" there would make the
# directory unreachable even by explicit subpath, and report it as "secret/excluded".
# These are pruned from the RECURSIVE grep walk only, and every prune is reported back in
# dirs_skipped so a caller is never silently given a partial corpus.
_SB_WALK_SKIP = {"logs", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                 # 2026-09-09: historical Kilo run-logs. Measured in bifrost-shell-mcp:v11
                 # over the production bind mount: 12,289 of autoagent's 18,556 enumerated
                 # files (-66.2%) and 2,947 of its 3,179 visited directories (-92.7%),
                 # cutting one 0-match grep_repo call from ~198s to ~39s. Zero files under
                 # them carry a code extension -- they are .md/.log run-logs.
                 # These are matched as BARE NAMES (`if d in _SB_WALK_SKIP`), never as
                 # paths, so all four are required: "archive" alone does NOT catch
                 # "archive52b". bifrost-router has no archive* directory and is
                 # unaffected. Every prune is still reported in dirs_skipped, and each
                 # directory stays fully searchable by passing it as an exact subpath.
                 "archive", "archive52", "archive52b", "archiveG2"}
_SB_GREP_MAX = 100
# Matches are COLLECTED up to _SB_GREP_MAX, but COUNTING continues past it so a caller gets a
# denominator instead of a meaningless "100". Counting is NOT free -- it turns an early exit
# into a full scan (measured 2026-09-05: 0.16s -> 7.46s on bifrost-router, 0.07s -> 162.5s on
# autoagent, which alone would breach the 200s AUTOPILOT_LEG_INNER_TIMEOUT). So the count
# phase is bounded by a wall-clock budget; when it expires total_matches is declared a LOWER
# BOUND (total_matches_exact False) rather than being passed off silently as a total.
_SB_GREP_COUNT_BUDGET_S = 20.0
# 2026-09-09: bounds the WHOLE scan, not just the post-100-match count phase. The budget
# below is armed only inside the >=_SB_GREP_MAX branch, so a query that never reaches 100
# matches had no bound at all -- measured in bifrost-shell-mcp:v11, a 0-match autoagent
# scan read all 18,554 files in 170s with nothing watching the clock, while a >100-match
# scan on the same corpus was stopped at 2,706 files in 20.1s. A 0-match query is the one
# that MUST read the entire corpus, so it is the one that most needs a deadline.
_SB_GREP_SCAN_BUDGET_S = 45.0
_SB_LINE_MAX = 300
_SB_LIST_MAX = 300
_SB_GIT_MAX = 16000
_SB_FILE_MAX_BYTES = 2_000_000
_SB_FILE_COUNT_CAP = 20000
_SB_SECRET_CONTENT = [
    _re_sb.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    _re_sb.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    _re_sb.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),
    _re_sb.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # 2026-09-09. Was `sk-[A-Za-z0-9]{20,}\b`: it needs 20+ UNBROKEN alphanumerics after
    # "sk-", so hyphen-segmented modern keys (sk-or-v1-, sk-ant-api03-) died at the first
    # segment and were never redacted.
    _re_sb.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    _re_sb.compile(r"\bK10[0-9a-f]{16,}::"),                    # k3s node token
    _re_sb.compile(r"\b\d{8,12}:AA[A-Za-z0-9_-]{30,}"),         # telegram bot token
    # 2026-09-09. The name was anchored with \b, and \b DOES NOT MATCH between "_" and a
    # letter -- "_" is a word character, so in K3S_TOKEN there is no boundary before TOKEN
    # and this could not fire. Every SCREAMING_SNAKE env-var name, which is how essentially
    # every credential on this fleet is written, walked past it: K3S_TOKEN,
    # OPENROUTER_API_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN all LEAKED into tool output
    # that feeds a model. The name may sit INSIDE an identifier, so match it there. Same
    # fault and same fix as the pre-push gate (autoagent 7d4c33a); shapes mirrored from
    # coordination/canary/secret_shapes.py.
    #
    # `token(?!s)`: the PLURAL is a COUNT, not a credential. Without it this table redacts
    # max_tokens= / prompt_tokens / n_cache_tokens -- measured +26 lines on the autoagent
    # working tree, nearly all legitimate Python a model then cannot read. With it, +9.
    #
    # NO placeholder filter here, unlike secret_shapes.py, and that divergence is
    # deliberate: a pre-push gate that blocks a documented example teaches people to use
    # the bypass, but on THIS surface over-redacting a placeholder costs a model one dull
    # line while a real credential dressed as "...-example" would walk straight out.
    _re_sb.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.\-]{0,40}?"
                   r"(?:api[_-]?key|secret|token(?!s)|password|passwd|pwd|credential|"
                   r"access[_-]?key|private[_-]?key)[A-Za-z0-9_.\-]{0,20}"
                   r"\s*[=:]\s*['\"]?[A-Za-z0-9\-._/+=:]{8,}"),
    # 2026-08-04 (audit L1): was {40,}, which redacted every 40-char git SHA-1. Raise the
    # floor above SHA-1(40)/SHA-256(64) so real high-entropy secrets still match but commit
    # hashes survive. The other six content patterns and the path/filename denies are unchanged.
    # 2026-08-04 (ai-review #1): A1/D4 raised this from {40,} to {65,} to stop redacting
    # 40-char git SHA-1 commit hashes. That also stopped redacting ALL bare hex of 40-64
    # chars -- including SHA-256-length API keys and webhook secrets that carry no sk-/xox*
    # prefix and no key=value shape, so nothing else catches them. Real DLP loosening on a
    # tool whose output is read by an LLM. Fixed by excluding EXACTLY 40 instead of raising
    # the floor: commit hashes stay visible, everything from 41 up is redacted again.
    _re_sb.compile(r"\b(?![A-Fa-f0-9]{40}\b)[A-Fa-f0-9]{40,}\b"),
]


def _sb_redact(text: str) -> str:
    """Defense-in-depth: replace secret-looking VALUES with a marker before returning."""
    if not text:
        return text
    for pat in _SB_SECRET_CONTENT:
        text = pat.sub("[REDACTED-SECRET]", text)
    return text


def _sb_name_is_secret(name: str) -> bool:
    low = name.lower()
    return any(_fnmatch_sb.fnmatch(low, p) for p in _SB_SECRET_NAME_DENY)


def _sb_resolve(repo: str, subpath: str):
    """Resolve repo+subpath under an allow-listed root. Returns (root, target, None) or
    (None, None, error). Blocks traversal escape + any secret/excluded path segment."""
    root = _SB_REPO_ROOTS.get(repo)
    if root is None:
        return None, None, f"[DENIED: unknown repo '{repo}'; allowed: {sorted(_SB_REPO_ROOTS)}]"
    sub = (subpath or "").strip().replace("\\", "/").lstrip("/")
    try:
        root_r = root.resolve()
        target = (root_r / sub).resolve()
        rel = target.relative_to(root_r)   # raises ValueError if target escaped the root
    except Exception:
        return None, None, f"[DENIED: path '{subpath}' escapes repo root '{repo}']"
    for seg in rel.parts:
        if seg in _SB_DIR_DENY or _sb_name_is_secret(seg):
            return None, None, f"[DENIED: secret/excluded path segment '{seg}']"
    return root_r, target, None


# ---------------------------------------------------------------------------
# Tool: run_ruff
# ---------------------------------------------------------------------------

@mcp.tool()
@gated_tool("ANALYZE")
def run_ruff(code: str, filename: str = "generated.py") -> dict:
    """
    Run ruff linter on Python code string.
    
    Args:
        code: Python source code to lint
        filename: Virtual filename for error messages (default: generated.py)
    
    Returns:
        {
            "passed": bool,
            "violations": [{"line": int, "col": int, "code": str, "message": str}],
            "violation_count": int,
            "summary": str
        }
    """
    return _run_ruff(code, filename)


# ---------------------------------------------------------------------------
# Tool: run_mypy
# ---------------------------------------------------------------------------

@mcp.tool()
@gated_tool("ANALYZE")
def run_mypy(code: str, filename: str = "generated.py") -> dict:
    """
    Run mypy type checker on Python code string.
    
    Args:
        code: Python source code to type-check
        filename: Virtual filename for error messages (default: generated.py)
    
    Returns:
        {
            "passed": bool,
            "errors": [{"line": int, "severity": str, "message": str}],
            "error_count": int,
            "summary": str
        }
    """
    return _run_mypy(code, filename)


# ---------------------------------------------------------------------------
# Tool: run_checks (combined)
# ---------------------------------------------------------------------------

@mcp.tool()
@gated_tool("ANALYZE")
def run_checks(code: str, filename: str = "generated.py") -> dict:
    """
    Run both ruff and mypy on Python code. Primary tool for Verification Harness Level 3.
    
    Args:
        code: Python source code to validate
        filename: Virtual filename for error messages
    
    Returns:
        {
            "passed": bool,          # True only if BOTH ruff and mypy pass
            "ruff": {...},           # Full ruff result
            "mypy": {...},           # Full mypy result  
            "total_issues": int,     # ruff violations + mypy errors
            "summary": str           # "PASS" or "FAIL: ruff(N) mypy(M)"
        }
    """
    return _run_checks(code, filename)


# ---------------------------------------------------------------------------
# BIFROST Infrastructure MCP Tools (Session H)
# ---------------------------------------------------------------------------

@mcp.tool()
@gated_tool("READ")
async def router_health() -> str:
    """Check BIFROST Router health and current mode."""
    import httpx
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(resolve("router") + "/health")
        return r.text


@mcp.tool()
@gated_tool("READ")
async def router_status() -> str:
    """Get full BIFROST Router status including tier assignments."""
    import httpx
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(resolve("router") + "/status")
        return r.text


def _sb_commit_total(root: Path):
    """Commits on HEAD, or None if it cannot be determined.

    Measured 2026-09-07: `git log -2` and a two-commit repo are byte-identical in this
    tool's result, so the lane model reported autoagent as having 2 commits against a
    true 832 -- the caller's own bound read back as the total. grep_repo returns
    total_matches and list_dir returns total for exactly this reason; the git tools were
    missed. rev-list is read-only, list-form, and scoped to an allow-listed root like
    every other git call here."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        s = (r.stdout or "").strip()
        return int(s) if r.returncode == 0 and s.isdigit() else None
    except Exception:
        return None


@mcp.tool()
@gated_tool("READ")
async def git_status(repo: str = "bifrost-router") -> str:
    """Git status of an allow-listed repo (short form).

    2026-08-04: was hardcoded to a HOST path that does not exist in the container, so this
    always returned "fatal: cannot change to ...". Now scoped to _SB_REPO_ROOTS like git_read. The repo is mounted READ-ONLY, so git cannot refresh its index cache; --no-optional-locks + -uno keep this from timing out.
    """
    root = _SB_REPO_ROOTS.get(repo)
    if root is None:
        return f"[DENIED: unknown repo '{repo}'; allowed: {sorted(_SB_REPO_ROOTS)}]"
    if not root.exists():
        return f"[not found: repo '{repo}' (is the repo mount present?)]"
    # NOTE (measured 2026-08-04): this repo is mounted :ro from Windows and `git status`
    # costs ~20s on autoagent (3133 tracked files) -- it is stat latency across the bind
    # mount, NOT index writes. A writable GIT_INDEX_FILE cache was tried and made no
    # difference (warm 19s vs cold 20s), so it was removed rather than left as dead
    # complexity plus a read-write mount. Hence the generous timeout below.
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), "status", "--short", "-uno"],
        capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    return _sb_redact((result.stdout or "") + (result.stderr or ""))[:_SB_GIT_MAX]


@mcp.tool()
@gated_tool("READ")
async def git_log(n: int = 10, repo: str = "bifrost-router") -> str:
    """Recent git log for an allow-listed repo.

    2026-08-04: same hardcoded-host-path defect as git_status, but silent -- it returned
    only stdout, so the failure surfaced as an EMPTY string rather than an error.
    """
    root = _SB_REPO_ROOTS.get(repo)
    if root is None:
        return f"[DENIED: unknown repo '{repo}'; allowed: {sorted(_SB_REPO_ROOTS)}]"
    if not root.exists():
        return f"[not found: repo '{repo}' (is the repo mount present?)]"
    try:
        _n = max(1, min(int(n), 200))
    except Exception:
        _n = 10
    result = subprocess.run(
        ["git", "-C", str(root), "log", "--oneline", f"-{_n}"],
        capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=10,
        encoding="utf-8", errors="replace",
    )
    out = (result.stdout or "") or f"[git log failed rc={result.returncode}: {(result.stderr or '')[:200]}]"
    _total = _sb_commit_total(root)
    if _total is not None:
        out += (f"\n[You asked for {_n}. This repo has {_total} commits on HEAD. This "
                f"listing is bounded by your n, NOT by the history -- do not report "
                f"{_n} as a count.]")
    return _sb_redact(out)[:_SB_GIT_MAX]


@mcp.tool()
@gated_tool("READ")
async def kb_search(query: str, n_results: int = 5, project: str = "bifrost-self") -> str:
    """Search bifrost-kb RAG knowledge base."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            resolve("kb") + "/retrieve",
            json={"question": query, "project": project, "top_k": n_results}
        )
        return r.text


@mcp.tool()
@gated_tool("EXECUTE")
async def autopilot_submit(prompt: str) -> str:
    """Submit a task to BIFROST AUTOPILOT."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            resolve("router") + "/autopilot/submit",
            json={"prompt": prompt}
        )
        return r.text


@mcp.tool()
@gated_tool("READ")
async def autopilot_status(graph_id: str) -> str:
    """Check AUTOPILOT job status."""
    import httpx
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(resolve("router") + f"/autopilot/{graph_id}/status")
        return r.text


@mcp.tool()
@gated_tool("READ_FS")
async def read_file(repo: str = "bifrost-router", subpath: str = "") -> str:
    """Read ONE file from an allow-listed repo (scoped, secret-denied, redacted, capped).

    2026-08-04 SECURITY (audit C2): the previous signature took a free-form absolute `path`
    and returned ANY file on the host -- bypassing the path-scope / secret-deny / redaction
    guarantees this module's own header states. Now routed through _sb_resolve like
    grep_repo/list_dir, so those guarantees hold uniformly.
    """
    root, target, err = _sb_resolve(repo, subpath)
    if err:
        return err
    if not target.exists() or not target.is_file():
        return f"[not found: {repo}/{subpath}]"
    if _sb_name_is_secret(target.name):
        return f"[DENIED: secret-looking filename '{target.name}']"
    try:
        if target.stat().st_size > _SB_FILE_MAX_BYTES:
            return f"[DENIED: file too large (> {_SB_FILE_MAX_BYTES} bytes)]"
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[read error: {type(e).__name__}: {str(e)[:120]}]"
    return _sb_redact(text)[:_SB_GIT_MAX]


@mcp.tool()
@gated_tool("WRITE")
async def write_file(path: str, content: str) -> str:
    """Write content to a file on Bifrost."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"OK: wrote {len(content)} chars to {path}"


@mcp.tool()
@gated_tool("EXECUTE")
async def run_python(code: str, cwd: str = r"D:\Projects\bifrost-router") -> str:
    """Execute Python code in the bifrost-router context."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        result = subprocess.run(
            ["python", tmp],
            capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=30, cwd=cwd
        )
        return result.stdout + result.stderr
    finally:
        os.unlink(tmp)


# ---------------------------------------------------------------------------
# Stack B read-only INVESTIGATE tools (scoped + secret-denied + capped + read-only)
# ---------------------------------------------------------------------------

@mcp.tool()
@gated_tool("READ_FS")
async def grep_repo(pattern: str, repo: str = "bifrost-router", subpath: str = "") -> dict:
    """Read-only regex grep over an allow-listed repo (scoped, secret-denied, capped).

    repo in {bifrost-router, autoagent}; subpath scopes within it. Returns file:line matches
    (paths relative to the repo root). Secret files/dirs are skipped; secret-looking values
    are redacted. Read-only (os.walk + re; never a subprocess/shell)."""
    root, base, err = _sb_resolve(repo, subpath)
    if err:
        return {"error": err, "denied": True, "matches": []}
    try:
        rx = _re_sb.compile(pattern)
    except _re_sb.error as e:
        return {"error": f"[bad regex: {e}]", "matches": []}
    if not base.exists():
        return {"error": f"[not found: {repo}/{subpath} (is the repo mount present?)]", "matches": []}

    files_to_scan = []
    files_capped = False
    dirs_skipped = set()
    if base.is_file():
        if not _sb_name_is_secret(base.name):
            files_to_scan = [base]
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            for d in dirnames:
                if d in _SB_WALK_SKIP:
                    dirs_skipped.add(d)
            dirnames[:] = [d for d in dirnames
                           if d not in _SB_DIR_DENY and d not in _SB_WALK_SKIP
                           and not _sb_name_is_secret(d)]
            for fn in filenames:
                if not _sb_name_is_secret(fn):
                    files_to_scan.append(Path(dirpath) / fn)
            if len(files_to_scan) >= _SB_FILE_COUNT_CAP:
                files_capped = True
                break

    matches, truncated = [], False
    total_matches = 0
    files_read = 0
    count_exhausted = False
    scan_exhausted = False
    # Armed BEFORE the loop, not inside the >=_SB_GREP_MAX branch.
    _count_deadline = time.monotonic() + _SB_GREP_SCAN_BUDGET_S
    for fp in files_to_scan:
        try:
            if fp.stat().st_size > _SB_FILE_MAX_BYTES:
                continue
            relr = str(fp.relative_to(root))
            files_read += 1
            with fp.open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        total_matches += 1
                        if len(matches) < _SB_GREP_MAX:
                            matches.append({"file": relr, "line": i,
                                            "text": _sb_redact(line.rstrip("\n"))[:_SB_LINE_MAX]})
                            if len(matches) >= _SB_GREP_MAX:
                                truncated = True
                                # tighten once a full page is collected: the rest is
                                # counting only. min(), so this can never EXTEND the
                                # scan budget past the point set before the loop.
                                _count_deadline = min(
                                    _count_deadline,
                                    time.monotonic() + _SB_GREP_COUNT_BUDGET_S)
        except Exception:
            continue
        if _count_deadline is not None and time.monotonic() > _count_deadline:
            if truncated:
                count_exhausted = True
            else:
                scan_exhausted = True
            break
    total_exact = not (count_exhausted or files_capped or scan_exhausted)
    out = {"repo": repo, "pattern": pattern, "match_count": len(matches),
           "total_matches": total_matches, "total_matches_exact": total_exact,
           "truncated": truncated, "files_scanned": len(files_to_scan),
           "files_read": files_read,
           "files_capped": files_capped, "matches": matches}
    if dirs_skipped:
        out["dirs_skipped"] = sorted(dirs_skipped)
        out["dirs_skipped_note"] = (
            f"[NOT SEARCHED: {sorted(dirs_skipped)} were skipped for speed (generated "
            f"output/caches). They are still reachable by passing subpath explicitly.]")
    if truncated:
        if total_exact:
            out["truncated_note"] = (
                f"[SHOWING {len(matches)} of {total_matches} TOTAL matches. The whole corpus "
                f"({files_read} files) WAS counted, so {total_matches} is EXACT. Answer "
                f"'how many' with total_matches -- match_count is only how many are LISTED.]")
        else:
            out["truncated_note"] = (
                f"[SHOWING {len(matches)} matches; AT LEAST {total_matches} exist. That count "
                f"is a LOWER BOUND -- counting stopped after {_SB_GREP_COUNT_BUDGET_S:.0f}s "
                f"having read {files_read} of {len(files_to_scan)} enumerated files. To answer "
                f"'how many matches' use total_matches ({total_matches}); files_read counts "
                f"FILES, not matches, and is NOT the answer to that question. Narrow with "
                f"subpath for an exact total.]")
    if files_capped:
        out["incomplete_scan"] = (
            f"[SCAN INCOMPLETE: stopped after {len(files_to_scan)} files "
            f"(cap {_SB_FILE_COUNT_CAP}). Files past that point were NOT searched, "
            f"so a low or zero match_count is NOT evidence of absence. Re-run with "
            f"a narrower subpath.]")
    if scan_exhausted:
        out["incomplete_scan_budget"] = (
            f"[SCAN INCOMPLETE: the {_SB_GREP_SCAN_BUDGET_S:.0f}s scan budget expired "
            f"after reading {files_read} of {len(files_to_scan)} enumerated files. A low "
            f"or ZERO match_count is NOT evidence of absence. Narrow with subpath and "
            f"re-run.]")
    return out


@mcp.tool()
@gated_tool("READ_FS")
async def list_dir(repo: str = "bifrost-router", subpath: str = "") -> dict:
    """Read-only directory listing of an allow-listed repo (scoped, secret-denied, capped).
    Secret/excluded entries are elided (never listed)."""
    root, base, err = _sb_resolve(repo, subpath)
    if err:
        return {"error": err, "denied": True, "entries": []}
    if not base.exists():
        return {"error": f"[not found: {repo}/{subpath} (is the repo mount present?)]", "entries": []}
    if not base.is_dir():
        return {"error": f"[not a directory: {repo}/{subpath}]", "entries": []}
    entries, truncated, total = [], False, 0
    try:
        eligible = [c for c in sorted(base.iterdir(), key=lambda c: c.name)
                    if c.name not in _SB_DIR_DENY and not _sb_name_is_secret(c.name)]
        total = len(eligible)
        truncated = total > _SB_LIST_MAX
        for child in eligible[:_SB_LIST_MAX]:
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
    except Exception as e:
        return {"error": f"[list error: {type(e).__name__}: {str(e)[:120]}]", "entries": entries}
    out = {"repo": repo, "subpath": subpath, "count": len(entries),
           "total": total, "truncated": truncated, "entries": entries}
    if truncated:
        out["truncated_note"] = (
            f"[LISTING TRUNCATED: showing {len(entries)} of {total} entries (cap "
            f"{_SB_LIST_MAX}). The remaining {total - len(entries)} were NOT returned. "
            f"Pass a narrower subpath to see them.]")
    return out


@mcp.tool()
@gated_tool("READ_FS")
async def git_read(repo: str = "bifrost-router", mode: str = "log", args: str = "") -> dict:
    """Read-only git inspection of an allow-listed repo. mode in {log, show, diff} ONLY
    (never a mutating verb). Extra args are sanitized (read flags + ref/path tokens only);
    output is secret-redacted + capped. subprocess list-form (no shell)."""
    root = _SB_REPO_ROOTS.get(repo)
    if root is None:
        return {"error": f"[DENIED: unknown repo '{repo}'; allowed: {sorted(_SB_REPO_ROOTS)}]", "denied": True}
    root_r = root.resolve()
    if not root_r.exists():
        return {"error": f"[not found: repo '{repo}' (is the repo mount present?)]"}
    if mode not in ("log", "show", "diff"):
        return {"error": f"[DENIED: mode must be log|show|diff, got '{mode}']"}
    _allowed_flags = {"--oneline", "--stat", "--name-only", "--name-status", "--numstat",
                      "-p", "--no-color", "--graph", "--decorate", "--"}
    safe, dropped = [], []
    for tok in (args or "").split()[:24]:
        if tok.startswith("-"):
            if tok.split("=")[0] in _allowed_flags or _re_sb.fullmatch(r"-\d{1,5}", tok):
                safe.append(tok)
            else:
                dropped.append(tok)
        elif _re_sb.fullmatch(r"[A-Za-z0-9_./~^@:+-]{1,200}", tok):
            safe.append(tok)
        else:
            dropped.append(tok)
        # dropped tokens are REPORTED in the result: a silently discarded flag let a caller
        # certify a request the tool never honored. -n/--max-count are NOT allowed here;
        # the only surviving count form is a bare -N.
    # ITEM 7: `mode` ALREADY supplies the subcommand. A leading log/show/diff token in `args`
    # builds `git show show <sha>`, which exits 128 with EMPTY output -- byte-identical to a
    # genuinely missing object. Measured 2026-09-05: that ambiguity made a model report commits
    # that DO exist as "does not exist" (3 of 9 reps). Strip it and declare it.
    redundant_verb = None
    if safe and safe[0] in ("log", "show", "diff"):
        redundant_verb = safe.pop(0)
    try:
        result = subprocess.run(
            ["git", "-C", str(root_r), mode, *safe],
            capture_output=True, stdin=subprocess.DEVNULL, text=True,
            timeout=15, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        return {"error": f"[git error: {type(e).__name__}: {str(e)[:150]}]"}
    out = _sb_redact(result.stdout or "")
    full_chars = len(out)
    git_truncated = full_chars > _SB_GIT_MAX
    if len(out) > _SB_GIT_MAX:
        out = out[:_SB_GIT_MAX] + "\n... [truncated]"
    res = {"repo": repo, "mode": mode, "args": " ".join(safe),
           "returncode": result.returncode, "output": out}
    if mode == "log":
        _refs = [t for t in safe if not t.startswith("-")]
        if _refs:
            res["total_commits"] = None
            res["total_commits_note"] = (
                f"[NOT COUNTED: args name a ref or path ({' '.join(_refs)}), so a HEAD "
                f"count would not describe this log. What is shown is what you "
                f"REQUESTED, not what exists.]")
        else:
            _total = _sb_commit_total(root_r)
            res["total_commits"] = _total
            if _total is not None:
                res["total_commits_note"] = (
                    f"[This repo has {_total} commits on HEAD. `-N` bounds the OUTPUT, "
                    f"not the history: the number of commits shown is what you asked "
                    f"for.]")
    if git_truncated:
        res["truncated"] = True
        res["output_chars"] = _SB_GIT_MAX
        res["full_chars"] = full_chars
        res["truncated_note"] = (
            f"[OUTPUT TRUNCATED at {_SB_GIT_MAX} of {full_chars} chars. The rest was NOT "
            f"returned, so any count derived from this output is a LOWER BOUND.]")
    if redundant_verb is not None:
        res["redundant_subcommand"] = redundant_verb
        res["redundant_subcommand_note"] = (
            f"[REMOVED a redundant '{redundant_verb}' from args: mode already supplies the "
            f"subcommand, and leaving it would have run `git {mode} {redundant_verb} ...`, "
            f"which fails with rc=128 and empty output -- indistinguishable from a missing "
            f"ref. Pass the bare ref in args.]")
    if result.returncode != 0 and not (out or "").strip():
        res["empty_failure_note"] = (
            f"[rc={result.returncode} with EMPTY output. This does NOT establish that the ref "
            f"is missing -- a malformed call fails IDENTICALLY. Before reporting that anything "
            f"does not exist, re-run with args set to the BARE ref and confirm a known-good ref "
            f"returns rc=0 through the same call shape.]")
    if dropped:
        res["dropped"] = dropped
        res["dropped_note"] = (
            f"[ARGS DROPPED by the sanitizer: {dropped}. git did NOT receive them, so this "
            f"result does NOT reflect those flags. For a commit count use a bare -N such as "
            f"-2; -n 2 and --max-count=2 are dropped.]")
    return res


# ---------------------------------------------------------------------------
# Tool: delegate_subtask (DELEGATE class -- network call to a peer leaf)
# ---------------------------------------------------------------------------
# 5of5 (2026-06-26): lets a tool-capable orchestrator (Igor) fan a subtask out to
# a named leaf via the Router's OpenAI-compatible chat endpoint -- the same
# mechanism as the bifrost-delegate MCP delegate() tool. The delegated leaf
# receives a PLAIN completion (no tools): this delegates REASONING to a peer, not
# tool-calling. Gated under DELEGATE (network only; no subprocess, no host FS write).
#
# SAFE_LEAVES allowlist (2026-09-11). This tool POSTed ANY leaf_alias to the Router, so
# bifrost-auto, the cloud tiers and claude-* were all dispatchable from here. The allowlist
# has ONE source: SAFE_LEAVES in bifrost_delegate.py, the sibling file, which the
# bifrost-delegate MCP delegate() enforces too. It is read with ast and NOT imported:
# importing bifrost_delegate GETs /v1/models and builds a second FastMCP server. Re-read on
# every call, so an edit to SAFE_LEAVES needs no restart here. FAIL-CLOSED: if the file is
# missing or SAFE_LEAVES cannot be read, EVERY leaf is refused -- so the image that runs
# this server must ship bifrost_delegate.py next to this file.
import ast as _ast_sl
from shared.resolver import resolve

_SAFE_LEAVES_SRC = Path(__file__).parent / "bifrost_delegate.py"


def _safe_leaves():
    """(frozenset of SAFE_LEAVES keys, "") or (None, reason). Never raises."""
    try:
        tree = _ast_sl.parse(_SAFE_LEAVES_SRC.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, _ast_sl.Assign) and any(
                    isinstance(t, _ast_sl.Name) and t.id == "SAFE_LEAVES" for t in node.targets):
                table = _ast_sl.literal_eval(node.value)
                if isinstance(table, dict) and table and all(isinstance(k, str) for k in table):
                    return frozenset(table), ""
                return None, "SAFE_LEAVES is not a non-empty dict with str keys"
        return None, "no SAFE_LEAVES assignment found"
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def _extract_router_error(exc: Exception):
    """F-4 (2026-09-11): extract (status_code, parsed_error_object) from HTTP error.
    Retains Router's structured error object (bounded) instead of discarding raw body."""
    import httpx
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
    return None, f"{type(exc).__name__}: {str(exc)[:200]}"


@mcp.tool()
@gated_tool("DELEGATE")
async def delegate_subtask(leaf_alias: str, subtask: str, timeout: int = 120) -> str:
    """Delegate a subtask to a named BIFROST leaf executor via the Router.

    Args:
        leaf_alias: Router alias (e.g. bifrost-coder-toolmaster, bifrost-1a-hearth,
                    bifrost-toolmaster, bifrost-coder, bifrost-igor-solo). MUST be a
                    key of SAFE_LEAVES in bifrost_delegate.py; any other alias is
                    refused before the Router is called.
        subtask:    The prompt/task text to deliver to the leaf.
        timeout:    Per-request timeout in seconds (default 120; use 240 for Igor).

    Returns:
        JSON string: {"ok": bool, "leaf": str, "response": str, "usage": dict,
        "elapsed_s": float, "error": str}. Never raises -- timeout, leaf-not-found /
        HTTP error, and parse failure all return ok=false with error populated.
    """
    # SAFE_LEAVES guard (2026-09-11): refuse before any network call. Exact match, no
    # case/whitespace folding -- the same comparison bifrost_delegate.delegate() makes.
    _safe, _why = _safe_leaves()
    if _safe is None or not isinstance(leaf_alias, str) or leaf_alias not in _safe:
        if _safe is None:
            _err = (f"SAFE_LEAVES allowlist unavailable from {_SAFE_LEAVES_SRC.name} ({_why}); "
                    f"refusing every leaf (fail-closed)")
        else:
            _err = (f"leaf {leaf_alias!r} is not in SAFE_LEAVES; refusing to dispatch. "
                    f"Pick one of: {sorted(_safe)}")
        return json.dumps({
            "ok": False,
            "leaf": leaf_alias if isinstance(leaf_alias, str) else repr(leaf_alias),
            "response": "", "usage": {}, "elapsed_s": 0.0, "error": _err,
        })
    import time as _t
    import httpx
    router_chat = resolve("router") + "/v1/chat/completions"
    payload = {
        "model": leaf_alias,
        "messages": [{"role": "user", "content": subtask}],
        "stream": False,
    }
    t0 = _t.monotonic()
    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            r = await client.post(router_chat, json=payload)
            r.raise_for_status()
            data = r.json()
        content = data["choices"][0]["message"]["content"]
        return json.dumps({
            "ok": True, "leaf": leaf_alias, "response": content,
            "usage": data.get("usage", {}),
            "elapsed_s": round(_t.monotonic() - t0, 2), "error": "",
        })
    except httpx.TimeoutException as e:
        return json.dumps({
            "ok": False, "leaf": leaf_alias, "response": "", "usage": {},
            "elapsed_s": round(_t.monotonic() - t0, 2),
            "error": f"timeout after {timeout}s ({type(e).__name__})",
        })
    except Exception as e:
        status, err_obj = _extract_router_error(e)
        res = {
            "ok": False, "leaf": leaf_alias, "response": "", "usage": {},
            "elapsed_s": round(_t.monotonic() - t0, 2),
            "error": err_obj,
        }
        if status is not None:
            res["status"] = status
        return json.dumps(res)

# ---------------------------------------------------------------------------
# Tool: quantum_circuit_test (COMPUTE class -- local-LAN compute, air-gap safe)
# ---------------------------------------------------------------------------
# S5 (2026-06-27): registers the local quantum-reward circuit as a self-maintaining
# tool. Maps an 8-bit quality x-vector -> QUBO -> QAOA -> Aer simulator on Forge
# (CPU-only, LAN-local SSH, NO internet egress = air-gap safe). Same proven path as
# session_o gate_7. FIXED command (no arbitrary code) -> gated under the new COMPUTE
# class, not EXECUTE. Never raises: bad input / Forge unreachable -> ok=false.

@mcp.tool()
@gated_tool("COMPUTE")
async def quantum_circuit_test(x_vector: str = "1,1,1,1,1,1,1,1") -> str:
    """Run the local QAOA quantum-reward circuit on Forge's Aer simulator.

    Args:
        x_vector: exactly 8 comma-separated binary ints (e.g. "1,1,0,1,1,1,0,1").

    Returns:
        JSON string: {"ok": bool, "reward": float|None, "x_vector": [...], "error": str}.
        Air-gap safe (LAN SSH to Forge; no internet). Never raises.
    """
    try:
        xs = [int(v) for v in x_vector.replace(" ", "").split(",") if v != ""]
    except ValueError:
        return json.dumps({"ok": False, "reward": None, "x_vector": [],
                           "error": "x_vector must be comma-separated ints"})
    if len(xs) != 8 or any(v not in (0, 1) for v in xs):
        return json.dumps({"ok": False, "reward": None, "x_vector": xs,
                           "error": "x_vector must be exactly 8 binary (0/1) values"})
    # NO DEFAULT HOST. This carried a `user@address` literal for one machine on one LAN.
    # An ssh target that silently falls back is worse than one that refuses: it reaches a
    # host somebody else owns, or hangs, and either way says nothing useful.
    forge_host = os.environ.get("BIFROST_FORGE_SSH", "").strip()
    if not forge_host:
        return json.dumps({"ok": False, "reward": None, "x_vector": xs,
                           "error": "BIFROST_FORGE_SSH is not set and no host is assumed; "
                                    "set it to user@host -- see .env.example"})
    forge_py = "~/quantum/venv/bin/python ~/autoagent/quantum_reward.py"
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           forge_host, f"{forge_py} --x-vector {','.join(map(str, xs))}"]
    try:
        result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                                text=True, timeout=25)
    except Exception as e:
        return json.dumps({"ok": False, "reward": None, "x_vector": xs,
                           "error": f"forge unreachable: {type(e).__name__}: {str(e)[:150]}"})
    if result.returncode != 0 or not (result.stdout or "").strip():
        return json.dumps({"ok": False, "reward": None, "x_vector": xs,
                           "error": f"forge rc={result.returncode}: {(result.stderr or '')[:150]}"})
    try:
        reward = float(json.loads(result.stdout.strip().splitlines()[-1]).get("reward"))
    except Exception as e:
        return json.dumps({"ok": False, "reward": None, "x_vector": xs,
                           "error": f"parse failed: {type(e).__name__}: {result.stdout[:120]}"})
    return json.dumps({"ok": True, "reward": reward, "x_vector": xs, "error": ""})


# ---------------------------------------------------------------------------
# SANDBOX-EXECUTION-PATH: isolated code execution in the k3d deny-all sandbox namespace.
# The SANDBOX policy class (see bifrost_shell_policy.json) is ENABLED where EXECUTE is DENIED,
# because this path is isolated-by-construction: the k3d `sandbox` namespace already enforces
# a deny-all NetworkPolicy + ResourceQuota + continuous sandbox_monitor.py auto-quarantine
# (Phases 1-3). This tool inherits those guardrails (it does not rebuild them): it lands a
# throwaway pod in that namespace, runs the code FAIL-CLOSED (payload runs only after egress
# deny-all is CONFIRMED -- guards the kube-router new-pod reconcile race), captures output,
# and always deletes the pod. Mirrors autoagent/sandbox_exec.py (proven 2026-07-04).
# ---------------------------------------------------------------------------

_SANDBOX_NS = "sandbox"
_SANDBOX_IMAGE = "python:3.12-slim"
_SANDBOX_ISOLATION_CONFIRM_S = 75
# Payload runs ONLY after an egress probe fails (deny-all active); else exits non-zero without
# running it (fail-closed -- untrusted code never executes with the network open).
_SANDBOX_BOOTSTRAP = (
    "import socket as _s, sys as _sys, time as _t\n"
    "def _eo():\n"
    "    try:\n"
    "        _s.setdefaulttimeout(2)\n"
    "        for _h,_p in (('1.1.1.1',53), ('192.168.x.4',5000), ('192.168.x.4',8089)):\n"
    "            try:\n"
    "                c=_s.create_connection((_h,_p)); c.close(); return True\n"
    "            except Exception: pass\n"
    "        return False\n"
    "    except Exception: return False\n"
    "_dl=_t.time()+%d\n"
    "while _eo():\n"
    "    if _t.time()>_dl:\n"
    "        print('SANDBOX_ISOLATION_NOT_CONFIRMED'); _sys.exit(3)\n"
    "    _t.sleep(2)\n"
) % _SANDBOX_ISOLATION_CONFIRM_S
# 2026-09-11 (Igor review follow-up, next to finding A10 of the 2026-09-10 harness review): no
# credential inside the payload pod. `kubectl run` gives the pod the namespace's default
# ServiceAccount, and the sandbox namespace leaves automountServiceAccountToken unset (= true), so
# the untrusted payload could read a live API token under
# /var/run/secrets/kubernetes.io/serviceaccount/ and print it to stdout, which this tool returns
# to the model. enableServiceLinks=false also drops the per-Service environment variables. (The
# MCP server's own environment never reaches the pod: kubectl run passes none to it.)
#
# Ceremony 14 (2026-09-22): ns sandbox now enforces PodSecurity `restricted`, and the spec
# above is REJECTED at admission -- measured against a real pod, not assumed:
#   pods "sbx-tool-..." is forbidden: violates PodSecurity "restricted:latest":
#   allowPrivilegeEscalation != false, unrestricted capabilities, runAsNonRoot != true,
#   seccompProfile
# Two things follow. allowPrivilegeEscalation and capabilities are CONTAINER-level fields and
# `kubectl run` names the container after the POD, so this can no longer be a module constant;
# it takes the pod name. And it is no longer a JSON MERGE: a merge patch REPLACES the
# containers list, so the pod is rejected for a missing image. The caller passes
# --override-type=strategic, which patches the container BY NAME. Both halves verified live.
def _sandbox_pod_overrides(pod: str) -> str:
    return json.dumps(
        {"apiVersion": "v1",
         "spec": {"automountServiceAccountToken": False,
                  "enableServiceLinks": False,
                  "securityContext": {"runAsNonRoot": True,
                                      "runAsUser": 1000,
                                      "seccompProfile": {"type": "RuntimeDefault"}},
                  "containers": [{"name": pod,
                                  "securityContext": {
                                      "allowPrivilegeEscalation": False,
                                      "capabilities": {"drop": ["ALL"]}}}]}},
        separators=(",", ":"))


@mcp.tool()
@gated_tool("SANDBOX")
async def run_in_sandbox(code: str, timeout_s: int = 120) -> str:
    """Execute Python `code` in the k3d deny-all `sandbox` namespace (no egress, resource-capped,
    no host FS, auto-deleted). Fail-closed: the payload runs only after network isolation is
    CONFIRMED. Returns JSON {ok, reason, stdout}. Use this for untrusted/generated code -- NOT
    run_python (which is host-scoped and EXECUTE-policy-denied)."""
    pod = "sbx-tool-" + uuid.uuid4().hex[:12]
    program = _SANDBOX_BOOTSTRAP + code
    overall = timeout_s + _SANDBOX_ISOLATION_CONFIRM_S

    def _kc(args, to):
        return subprocess.run(["kubectl"] + args, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=to)

    launched = False
    try:
        run = _kc(["run", pod, "-n", _SANDBOX_NS, "--image", _SANDBOX_IMAGE, "--restart=Never",
                   "--image-pull-policy", "IfNotPresent",
                   "--overrides", _sandbox_pod_overrides(pod),
                   "--override-type=strategic",
                   "--command", "--", "python", "-c", program], 60)
        if run.returncode != 0:
            return json.dumps({"ok": False, "reason": "launch_error", "stdout": (run.stderr or "")[:400]})
        launched = True
        deadline = time.time() + overall
        phase = ""
        while time.time() < deadline:
            phase = _kc(["get", "pod", pod, "-n", _SANDBOX_NS, "-o",
                         "jsonpath={.status.phase}"], 15).stdout.strip()
            if phase in ("Succeeded", "Failed"):
                break
            await asyncio.sleep(3)
        logs = _kc(["logs", pod, "-n", _SANDBOX_NS], 30)
        out = ((logs.stdout or "") + (logs.stderr or "")).strip()
        if phase == "Succeeded":
            return json.dumps({"ok": True, "reason": "succeeded", "stdout": out[:4000]})
        reason = ("isolation_not_confirmed" if "SANDBOX_ISOLATION_NOT_CONFIRMED" in out
                  else ("failed" if phase == "Failed" else "timeout"))
        return json.dumps({"ok": False, "reason": reason, "stdout": out[:4000]})
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "reason": "timeout", "stdout": ""})
    finally:
        if launched:
            try:
                _kc(["delete", "pod", pod, "-n", _SANDBOX_NS, "--ignore-not-found",
                     "--grace-period=0", "--force"], 30)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BIFROST Shell MCP Tool Server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8086)
    # 2026-08-04 SECURITY (audit C1): default to loopback. The previous 0.0.0.0 default
    # exposed all 19 MCP tools UNAUTHENTICATED to the LAN -- read_file returned arbitrary
    # host files (confirmed). Pass --host explicitly to bind wider, and add auth first.
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.transport == "http":
        print(f"[bifrost-shell] Starting HTTP server on {args.host}:{args.port}")
        mcp.run(transport="streamable-http", host=args.host, port=args.port, show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)



