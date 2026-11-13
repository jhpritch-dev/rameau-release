---
name: bifrost-shell
description: FastMCP server providing static analysis tools (ruff, mypy, run_checks) for the BIFROST Router codebase. Use when working on bifrost_shell.py, adding MCP tools, registering new servers in Continue.dev, or when the Verification Harness L3 static analysis tooling is relevant. Always use when the user mentions MCP tools, bifrost-shell, run_ruff, run_mypy, or adding new analysis tools.
---

# BIFROST Shell MCP Skill

`bifrost_shell.py` is a FastMCP HTTP server on `:8086`. Registered in Continue.dev and invokable by Claude Code for static analysis during AUTOPILOT subtask verification (Harness L3).

## Key file
`D:\Projects\bifrost-router\bifrost_shell.py`

## Current tools
- `run_ruff(path)` — lint check, readOnly+idempotent
- `run_mypy(path)` — type check, readOnly+idempotent  
- `run_checks(path)` — ruff + mypy combined

## Tool annotations (FastMCP pattern)
```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
async def run_ruff(path: str) -> str: ...
```

## Error message standard (mcp-builder pattern)
Return actionable errors, not raw output:
```
ruff: FAIL — 3 errors in auth.py:12,47,89. Run: ruff --fix auth.py
```
Not:
```
auth.py:12:1: E302 expected 2 blank lines, found 1
```

## Continue.dev registration
`~/.continue/mcpServers/bifrost-shell.json`
Transport: HTTP (not stdio) — required for parallel AUTOPILOT fan-out.

## Planned additional servers (Session S/C)
- `bifrost-rag` :8084 — ChromaDB query
- `bifrost-fs` :8085 — scoped filesystem read/write
- `bifrost-git` :8087 — git status, diff, commit

Read `D:\Projects\skills\skills\mcp-builder\reference\python_mcp_server.md` before building any new server.
