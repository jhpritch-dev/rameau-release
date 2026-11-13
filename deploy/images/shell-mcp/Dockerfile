# syntax=docker/dockerfile:1
# Dockerfile -- RAMEAU shell-MCP (static-analysis subset)
#
# The `# syntax=` directive MUST stay on line 1. A header comment placed above it silently
# disables it; the private original carries the same warning and it is kept here.
#
# Build context: services/shell-mcp/ in this repository -- exactly two files.
# Derived from: the private deployment's Dockerfile.bifrost-shell-mcp, which the assembly
#               landed twice (deploy/compose/shell-mcp/Dockerfile, now retired, and
#               harness/router/Dockerfile.bifrost-shell-mcp, kept as harness source).
#               Neither could build here, so this is a rewrite rather than a move.
#
# -----------------------------------------------------------------------------------------
# WHAT DIFFERS FROM THE PRIVATE ORIGINAL, AND WHY
# -----------------------------------------------------------------------------------------
# 1. THE CONTEXT IS A DEDICATED TWO-FILE DIRECTORY, not the router repository root.
#    The original built from the root "because checks.py / bifrost_shell_static.py live
#    there". In this release that root is harness/router/, which also holds
#    bifrost_shell_policy.json and capability_registry.json -- two of the three files that
#    landed under Rule 1. An ignore file would have denied them, but they would still have
#    been IN the context, and `docker build` reads the working tree rather than the index.
#    A context that cannot contain them is a stronger guarantee than one that excludes them.
#
# 2. UVICORN IS INSTALLED EXPLICITLY, and this is a correction rather than a preference.
#    The original's header states its dependency closure as "bifrost_shell_static.py imports
#    -> fastmcp, checks (nothing else)", and that is true of its TOP-LEVEL imports. It is not
#    true of the path this CMD takes: `import uvicorn` sits inside the http transport branch,
#    which `--transport http` is precisely what reaches. The original worked by inheriting
#    uvicorn transitively from fastmcp -- an implicit dependency on the serving path of a
#    server, which a future fastmcp is free to drop. `[standard]` rather than bare uvicorn,
#    the same way requirements.txt names it.
#
# 3. THE FASTMCP PIN IS THE RELEASE'S OWN. The original pins fastmcp==3.1.1 and says, in
#    terms, that this is matched to a specific host gateway and must not be "aligned" with
#    the router's pin. That host is not part of this release, so the instruction cannot be
#    honoured as written; requirements-mcp.txt already records 4.1.8 as this project's pin
#    for this server, and that is the one number here with a published basis. Recorded as a
#    SUBSTITUTION, not a silent carry-over: if the static subset turns out to need the 3.x
#    API, this is the line to change and the reason it was set is above it.
#
# 4. The base digest, the non-root user, EXPOSE and the CMD are unchanged.
#
# -----------------------------------------------------------------------------------------
# WHY THIS IS A SEPARATE IMAGE AT ALL
# -----------------------------------------------------------------------------------------
# fastmcp cannot co-install with the router's core pins -- requirements-mcp.txt records that
# as a measured ResolutionImpossible, and it is why conftest.py skips the tests that import
# it. A separate image is the resolution, not a workaround: the two dependency sets never
# have to agree because they never share an interpreter.

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

WORKDIR /app

# ---- layer 1: dependencies --------------------------------------------------------------
# ruff and mypy are the subset's actual work: bifrost_shell_static.py shells out to both on a
# tempfile. They are unpinned here exactly as in the original -- pinning a linter to a patch
# version pins its diagnostics too, and nothing in this release measures which ones it wants.
RUN pip install --no-cache-dir \
      "fastmcp==4.1.8" \
      "uvicorn[standard]>=0.27.0" \
      ruff \
      mypy

# ---- layer 2: the two source files ------------------------------------------------------
# Named individually rather than `COPY . .`: the context holds exactly these two, and naming
# them means a third file arriving in the context has to be added here deliberately.
COPY checks.py .
COPY bifrost_shell_static.py .

# Non-root. mypy writes .mypy_cache into /app and ruff writes a tempfile under /tmp; both are
# writable for this uid after the chown. If a build proves otherwise, drop the USER line --
# do not chmod 777 /app.
RUN useradd -m -u 10001 mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8086

CMD ["python", "bifrost_shell_static.py", "--transport", "http", "--port", "8086", "--host", "0.0.0.0"]
