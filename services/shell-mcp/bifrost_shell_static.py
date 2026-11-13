"""
bifrost_shell_static.py - BIFROST Shell MCP Tool Server (static-analysis subset)
================================================================================
Container-safe subset of bifrost_shell.py. Exposes ONLY the three host-independent
static-analysis tools (run_ruff, run_mypy, run_checks). The remaining 10 tools in
bifrost_shell.py hard-code Windows host paths / network endpoints and are unsafe to
expose from a container, so they are intentionally excluded here.

Tools:
  run_ruff   - lint Python code, return violations
  run_mypy   - type-check Python code, return errors
  run_checks - run both ruff + mypy, return combined pass/fail

Tool bodies are copied verbatim from bifrost_shell.py. The @gated_tool("ANALYZE")
policy wrapper is omitted: ANALYZE is always enabled in bifrost_shell_policy.json,
so the gate is a pass-through for these three tools, and this image ships no
WRITE/EXECUTE tools that the gate would otherwise deny. Dropping it removes the
dependency on the policy JSON without changing runtime behavior.

Usage:
  # Start server (stdio transport for LangGraph MCP adapter):
  python bifrost_shell_static.py

  # Or HTTP transport for direct calls / container:
  python bifrost_shell_static.py --transport http --port 8086 --host 0.0.0.0
"""

from fastmcp import FastMCP

# Shared static-analysis implementations (single source of truth, see checks.py).
# Imported as plain functions and re-exposed below through this server's own
# @mcp.tool() wrappers. This server intentionally omits the @gated_tool wrapper
# (ANALYZE is always enabled and it ships no WRITE/EXECUTE tools), so the bodies
# delegate straight to the shared implementations.
from checks import (
    run_ruff as _run_ruff,
    run_mypy as _run_mypy,
    run_checks as _run_checks,
)

mcp = FastMCP(
    name="bifrost-shell-static",
    instructions=(
        "Static analysis tools for BIFROST code verification. "
        "Use run_checks() to validate generated Python code before delivery. "
        "Returns structured pass/fail results with line-level error details."
    ),
)

# ---------------------------------------------------------------------------
# Tool: run_ruff
# ---------------------------------------------------------------------------

@mcp.tool()
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
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HTTP authentication (2026-10-13, RAMEAU containerization Finding 1)
# ---------------------------------------------------------------------------
# Measured 2026-10-05 against this image: with `--transport http` a request carrying no
# Authorization header could initialize, list and CALL all three tools. stdio opens no
# socket, so the live servers never needed this; the container does.
#
# Every HTTP and WebSocket request must carry `Authorization: Bearer <token>`. The token is  # rameau-scan: allow -- documents the header FORMAT with an angle-bracket placeholder; no value
# read from a FILE (a compose secret, mounted at /run/secrets/shell_mcp_token by default),
# never from an env var or a build arg, so it is absent from `docker inspect` and
# `docker history`. FAIL CLOSED: in http mode a missing, unreadable or short token file
# refuses to start rather than serving unauthenticated.

import hmac
import os
import sys

TOKEN_FILE_ENV = "SHELL_MCP_TOKEN_FILE"
DEFAULT_TOKEN_FILE = "/run/secrets/shell_mcp_token"
MIN_TOKEN_LEN = 32


def _load_token(path):
    """The token bytes, or None if the file is missing, unreadable or shorter than MIN_TOKEN_LEN."""
    try:
        with open(path, "rb") as fh:
            tok = fh.read().strip()
    except OSError:
        return None
    return tok if len(tok) >= MIN_TOKEN_LEN else None


class BearerAuth:
    """ASGI wrapper: 401 unless the Authorization header is exactly `Bearer <token>`.

    Lifespan events pass through untouched so the wrapped app still starts and stops."""

    def __init__(self, app, token):
        self.app = app
        self._expected = b"Bearer " + token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        got = b""
        for name, value in scope.get("headers") or ():
            if name.lower() == b"authorization":
                got = value
                break
        if hmac.compare_digest(got, self._expected):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"error":"unauthorized"}'
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BIFROST Shell MCP Tool Server (static subset)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8086)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    if args.transport == "http":
        token_file = os.environ.get(TOKEN_FILE_ENV, DEFAULT_TOKEN_FILE)
        token = _load_token(token_file)
        if token is None:
            print(f"[bifrost-shell-static] REFUSING to start: http mode needs a bearer token of at "
                  f"least {MIN_TOKEN_LEN} chars in {token_file} (set {TOKEN_FILE_ENV} to move it)",
                  file=sys.stderr)
            sys.exit(2)
        import uvicorn

        print(f"[bifrost-shell-static] Starting HTTP server on {args.host}:{args.port} (bearer auth)")
        uvicorn.run(BearerAuth(mcp.http_app(), token), host=args.host, port=args.port,
                    log_level="warning")
    else:
        mcp.run(transport="stdio", show_banner=False)
