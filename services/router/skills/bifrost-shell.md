# SKILL: bifrost-shell

## What This Is
FastMCP server exposing BIFROST infrastructure as Claude Code tools.
Running on Bifrost :8086.

## Available Tools
- router_health / router_status — check Router health and tier state
- run_ruff / run_mypy / run_checks — static analysis (original L3 tools)
- git_status / git_log — repo state
- kb_search — RAG search via bifrost-kb
- autopilot_submit / autopilot_status — AUTOPILOT task submission
- read_file / write_file — filesystem access on Bifrost
- run_python — execute Python in bifrost-router context

## Start Server
```
python D:\Projects\bifrost-router\bifrost_shell.py
python D:\Projects\bifrost-router\bifrost_shell.py --transport http --port 8086
```

## MCP Config Location
%APPDATA%\Claude\claude_desktop_config.json
