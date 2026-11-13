# Dockerfile -- bifrost-router / control plane
#
# Unit:          Router / control plane
# Source repo:   bifrost-router @ f5e6ca3  (single repo; NO bucket split in the capture)
# Build context: the bifrost-router repo ROOT, narrowed by the .dockerignore staged
#                beside this file (51 files + skills/, down from 578 / 19.6 MB).
# Staged from:   bifrost-router/Dockerfile (1404 B, PRE-EXISTING).
#
# ---------------------------------------------------------------------------
# WHAT CHANGED vs the pre-existing Dockerfile, and nothing else did
# ---------------------------------------------------------------------------
# 1. `COPY . .` -> 4 explicit COPY groups. The *content* reaching the image is decided by
#    the .dockerignore either way; the explicit COPYs make the layer boundaries match the
#    change frequency (deps < package < data < source) instead of invalidating everything
#    on any edit.
# 2. DELETED the line `COPY skills/ /app/skills/`. It was a no-op duplicate: the preceding
#    `COPY . .` already placed skills/ at /app/skills/ because the old .dockerignore
#    re-included it with `!skills/**`. Here skills/ is copied once, explicitly.
# 3. Removed the `# Place at: D:\Projects\bifrost-router\Dockerfile` header line. That is
#    the only drive literal that was in this file and it was a build-host instruction, not
#    a runtime path. The build command in BUILD-STAGING-2026-10-05.md replaces it.
# 4. Added HEALTHCHECK-equivalent note (not a HEALTHCHECK instruction -- k8s uses its own
#    probes and a docker HEALTHCHECK would be dead weight under the runtime descriptor).
# 5. 2026-10-15: pinned the base by digest -- `FROM python:3.11-slim@sha256:e88e9763...`.
#    The tag is kept beside the digest on purpose: the digest is what docker resolves,
#    the tag is what a human reads, and dropping it makes the line unreviewable.
# 6. 2026-10-15: made the pip step deterministic -- `--no-compile` plus a hash-based
#    compileall. Two --no-cache builds previously differed in 10,130 .pyc files whose
#    compiled code was byte-identical; only the embedded source mtime differed. With
#    this, plus SOURCE_DATE_EPOCH and rewrite-timestamp=true at export, two independent
#    builds produce the same OCI manifest digest. Measured, not assumed.
# The useradd/USER, EXPOSE, the BIFROST_COMMIT ARG/ENV/LABEL and the CMD are byte-for-byte
# the pre-existing ones. The FROM and the pip install no longer are -- see 5 and 6.
#
# ---------------------------------------------------------------------------
# THE TRAP THIS CONTEXT INTRODUCES -- do not ship without the leaf test
# ---------------------------------------------------------------------------
# bifrost_delegate.py is read off disk with ast by igor_tool_loop.py:1017; it is imported
# by nothing. A context built from an import closure alone omits it, and the tool loop then
# refuses EVERY delegated leaf, fail-closed, while /health still returns 200. It is in the
# allowlist. The acceptance test must exercise one delegated leaf, not just /health.
# Full reasoning: see the .dockerignore staged beside this file.

FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

WORKDIR /app

# ---- layer 1: dependencies (131 pins; changes rarely) ----------------------
COPY requirements.txt .
# `--no-compile` + an explicit hash-based compileall: pip's default compile stamps every
# .pyc with the source mtime, which makes two --no-cache builds differ in 10,130 files
# whose compiled code is identical. Hash-based invalidation is safe here because the
# image is immutable. -x keeps pip/_distutils_hack uncompiled, as pip itself leaves them.
# The image is still 257 .pyc larger than before (setuptools/wheel/pkg_resources, +1.5 MB):
# compileall reaches packages pip's own compile step skips. Nothing is lost and /app is
# byte-identical. Layer mtimes additionally need SOURCE_DATE_EPOCH and rewrite-timestamp=true
# at export -- that is a build-command property, not a Dockerfile one.
RUN pip install --no-cache-dir -r requirements.txt --no-compile && python -m compileall -q --invalidation-mode unchecked-hash -x '/(pip|_distutils_hack)/' /usr/local/lib/python3.11/site-packages

# ---- layer 2: the backends package ----------------------------------------
COPY backends/ /app/backends/

# ---- layer 3: runtime config read by path at import time -------------------
# fleet_config.json           config.py:178
# capability_registry.json    capability_registry.py:26
# capability_profiles.json    embed_similarity.py:41
# bifrost_shell_policy.json   policy_instructions.py:48
COPY fleet_config.json capability_registry.json capability_profiles.json bifrost_shell_policy.json ./

# ---- layer 4: the skill registry, baked per v114.10 PROGRESSIVE-SKILLS-1 ---
# (bind-mount becomes preferred when Session O P3 Memento-Skills generates skills at
#  runtime -- unchanged from the pre-existing Dockerfile's note)
COPY skills/ /app/skills/

# ---- layer 5: application source ------------------------------------------
# 39 modules in the import closure of main:app, plus bifrost_delegate.py which is
# ast-READ and not imported. The .dockerignore is what bounds this; the wildcard here
# cannot widen it.
COPY *.py ./

# Non-root user
RUN useradd -m bifrost && chown -R bifrost:bifrost /app
USER bifrost

EXPOSE 8080

# ROUTER-V-FINISH: bake the git SHA so /status reports the real commit (was "unknown").
# Pass --build-arg BIFROST_COMMIT=$(git rev-parse --short HEAD) at docker build time.
ARG BIFROST_COMMIT=unknown
ENV BIFROST_COMMIT=${BIFROST_COMMIT}
# Provenance label (2026-07-14): the deployed-source-in-git invariant checks THIS.
# A "-dirty" suffix (stamped by build.ps1 on an uncommitted tree) means the image
# matches no commit -- exactly the failure that shipped bifrost-kb/v214 image-only.
LABEL org.opencontainers.image.revision=${BIFROST_COMMIT}

# No HEALTHCHECK by design: k8s owns liveness/readiness for this unit under the runtime
# descriptor, and a docker HEALTHCHECK here would be a second, unreconciled opinion.

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75", "--loop", "asyncio"]
