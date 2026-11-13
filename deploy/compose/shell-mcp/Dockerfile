# syntax=docker/dockerfile:1
# Dockerfile.bifrost-shell-mcp  --  REFERENCE PATTERN for the BIFROST estate
#
# The `# syntax=` directive above MUST stay on line 1. It is carried over verbatim from the
# pre-existing file; a header comment placed above it silently disables it, which is a
# defect this staging very nearly introduced.
#
# Unit:          shell-MCP (static-analysis subset)
# Source repo:   bifrost-router @ f5e6ca3  (ALL files, one repo, no split)
#   EXCEPT bifrost_shell_static.py, which is HELD at RAMEAU's own f5763e0 and is NOT
#   refreshed from upstream. It carries the P2 shell-MCP authentication fix (Bearer
#   auth, MIN_TOKEN_LEN, TOKEN_FILE_ENV, ws close 1008) that was never upstreamed, so a
#   blind re-capture would revert a security fix to the vulnerable version. See
#   SOURCE-OF-RECORD section 9.
# Build context: the bifrost-router repo ROOT (checks.py / bifrost_shell_static.py live there)
# Staged from:   bifrost-router/Dockerfile.bifrost-shell-mcp  (586 B, PRE-EXISTING).
# EXACTLY THREE CHANGES vs the pre-existing file, each stated so none is silent:
#   1. added `RUN useradd -m -u 10001 mcp && chown -R mcp:mcp /app` + `USER mcp`
#      (the pre-existing file runs as root; every other Dockerfile in this estate --
#      router, arbiter, execsvc, telegram -- drops privilege, so this one was the outlier).
#      mypy writes .mypy_cache into /app and ruff writes a tempfile under /tmp; both are
#      writable for uid 10001 after the chown. If a build proves otherwise, drop the USER
#      line -- do not chmod 777 /app.
#   2. added this header, below the syntax directive.
#   3. 2026-10-15: pinned the base by digest --
#      `FROM python:3.13-slim@sha256:70729b46...`. Tag kept beside the digest so the
#      line stays reviewable.
# No COPY, EXPOSE, CMD or the syntax directive was altered. The FROM was -- see 3.
#
# WHY THIS IS THE REFERENCE
#   * one repo, one context, every COPY target resolves at the context root
#   * no host path, no drive letter, no LAN literal, no secret
#   * the tool surface is the container-safe subset by construction: bifrost_shell_static.py
#     exposes exactly run_ruff / run_mypy / run_checks and deliberately omits the
#     @gated_tool wrapper and every host-coupled tool in bifrost_shell.py
#
# VERIFIED AGAINST THE CAPTURE (not assumed):
#   bifrost_shell_static.py imports  -> fastmcp, checks  (nothing else)
#   checks.py imports                -> subprocess, sys, tempfile, os, re  (stdlib only)
#   so `fastmcp + ruff + mypy` is the complete and minimal dependency set. httpx is NOT
#   needed here (it is needed by the skills-MCP wrapper, a different unit).
#
# PIN NOTE: fastmcp==3.1.1 matches the HOST's fastmcp (audit MCP-DOCKER-0) and is
# deliberately NOT the router's requirements.txt pin (fastmcp==4.0.0b2). Do not "align"
# them -- this server talks to the host gateway, the router does not.

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

WORKDIR /app

RUN pip install --no-cache-dir fastmcp==3.1.1 ruff mypy

COPY checks.py .
COPY bifrost_shell_static.py .

# Non-root. The static subset shells out to ruff/mypy on a tempfile and needs nothing else.
RUN useradd -m -u 10001 mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8086

CMD ["python", "bifrost_shell_static.py", "--transport", "http", "--port", "8086", "--host", "0.0.0.0"]
