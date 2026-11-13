# SKILL: bifrost-shell

## What This Is
FastMCP tool server exposing static analysis tools as MCP-callable functions. Used by AUTOPILOT Verification Harness Level 3. Runs on Bifrost, exposes three tools: run_ruff, run_mypy, run_checks.

## Key Files
| File | Purpose |
|------|---------|
| `D:\Projects\bifrost-router\bifrost_shell.py` | FastMCP server — three tools |

## Tools
| Tool | What It Does | Returns |
|------|-------------|---------|
| `run_ruff(code, filename)` | Lint Python code string | `{passed, violations[], error_count}` |
| `run_mypy(code, filename)` | Type-check Python code string | `{passed, errors[], error_count}` |
| `run_checks(code, filename)` | Run ruff + mypy combined | `{passed, summary, ruff{}, mypy{}}` |

## Transport
- HTTP: `--transport http --port 8086` (for LangGraph MCP adapter)
- stdio: default (for Claude Code MCP config)

## Running
```powershell
# [Bifrost] HTTP transport (for AUTOPILOT):
python D:\Projects\bifrost-router\bifrost_shell.py --transport http --port 8086

# stdio transport (for Claude Code):
python D:\Projects\bifrost-router\bifrost_shell.py
```

## Integration Points
- AUTOPILOT `verify_subtask_node` Level 3 — replaces keyword heuristic with real ruff/mypy (pending Session H wire-up)
- Claude Code MCP config — `run_ruff` callable from IDE (pending Session H registration)
- LangGraph via `langchain-mcp-adapters` (pending Session H)

## Validated Behavior
- Bad code → `FAIL: mypy(1 errors)` with line-level detail
- Good code → `PASS: ruff clean, mypy clean`
- Syntax errors → fatal error detection (`error_count=1`)
- Windows drive-letter paths handled in mypy output parser (regex fix applied)

## Notes
- Writes code to tempfile, runs subprocess, parses stdout
- Fatal mypy errors (syntax) caught as `error_count=1` even with exit code 2
- Claude Code MCP registration pending — once wired, `run_checks` available inline in VS Code
