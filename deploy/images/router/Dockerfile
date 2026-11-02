# Dockerfile -- RAMEAU router
#
# Build context: services/router/ in this repository (the router source, after assembly).
# Derived from: the private deployment's router Dockerfile, which is in the release allowlist
#               as a straight copy. Three things had to change for the release; they are
#               listed below rather than left for a reader to infer from a diff they cannot
#               see, because the private original is not published.
#
# -----------------------------------------------------------------------------------------
# WHAT DIFFERS FROM THE PRIVATE ORIGINAL, AND WHY
# -----------------------------------------------------------------------------------------
# 1. THE FOUR RUNTIME JSON FILES ARE NO LONGER COPIED IN.
#    The original baked fleet_config.json, capability_registry.json, capability_profiles.json
#    and bifrost_shell_policy.json into the image. The release cannot: the allowlist disposes
#    all four as `derive-example`, because the real files describe a specific fleet. They are
#    bind-mounted from deploy/config/ instead -- see deploy/compose.yaml, which mounts each
#    one individually because each is read as `Path(__file__).parent / "<name>.json"`.
#    This is a genuine improvement rather than a workaround: fleet_config.json re-reads on
#    mtime change on every request, so a mounted file can be edited on a running router.
#
# 2. THE BUILD-HOST DRIVE LITERAL IS GONE, as it already was in the private staging copy.
#
# 3. THE COMMIT ARG IS RENAMED to RAMEAU_COMMIT, so the image label and the repository agree
#    on what this project is called.
#
# Everything else is deliberately unchanged: the digest-pinned base, the four COPY groups,
# the deterministic pip step, the non-root user, EXPOSE and the CMD.
#
# -----------------------------------------------------------------------------------------
# THIS IMAGE CANNOT BUILD YET -- requirements.txt is not in the release
# -----------------------------------------------------------------------------------------
# Layer 1 needs services/router/requirements.txt. The private repository's requirements.txt
# is dispositioned EXCLUDE_INTERNAL in the allowlist, so no dependency manifest has been
# selected for the release, and `docker build` will fail on the COPY below until one exists.
# That is left as a loud failure on purpose. The alternatives were to invent a pin set, with
# no way for a reviewer to tell which pins were measured; or to install unpinned, which makes
# two builds of one commit different images. A missing file that stops the build is the
# honest state of this gap.
#
# This comment quoted "131 dependency decisions" until 2026-11-02. That figure was another
# service's -- two bifrost-kb files summed, for a service not in this release -- and the
# router's own manifest is reported to be five range specifiers. Neither number is checkable
# from this repository, so none is quoted here as measured. The reasoning above never
# depended on the size.
#
# -----------------------------------------------------------------------------------------
# THE TRAP THIS CONTEXT INTRODUCES -- do not accept this image on /health alone
# -----------------------------------------------------------------------------------------
# bifrost_delegate.py is read off disk with `ast` and is imported by nothing. A build context
# computed from an import closure therefore omits it, and the tool loop then refuses EVERY
# delegated leaf, fail-closed, while /health still answers 200. The acceptance test has to
# exercise one delegated leaf. This is also why compose.yaml defines no healthcheck: a
# healthcheck on /health would read green on precisely this failure.

FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

WORKDIR /app

# ---- layer 1: dependencies (changes rarely) ---------------------------------------------
# `--no-compile` plus an explicit hash-based compileall. pip's default compile stamps every
# .pyc with the source mtime, which made two --no-cache builds of the private image differ in
# 10,130 files whose compiled code was byte-identical. Hash-based invalidation is safe here
# because the image is immutable. -x leaves pip and _distutils_hack uncompiled, as pip itself
# does. Reproducible layer mtimes additionally need SOURCE_DATE_EPOCH and
# rewrite-timestamp=true at export; those are build-command properties, not Dockerfile ones.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --no-compile \
 && python -m compileall -q --invalidation-mode unchecked-hash \
      -x '/(pip|_distutils_hack)/' /usr/local/lib/python3.11/site-packages

# ---- layer 2: the backends package ------------------------------------------------------
COPY backends/ /app/backends/

# ---- layer 3: the skill registry --------------------------------------------------------
COPY skills/ /app/skills/

# ---- layer 4: application source --------------------------------------------------------
# Dockerfile.dockerignore beside this file is what bounds this; the wildcard cannot widen
# it. That file is named for this one on purpose -- as plain `.dockerignore` it sat outside
# the build context and Docker read it not at all, which left this wildcard unbounded.
# Re-run the import-closure check after every source refresh: a module missing from the
# context does not fail the build, it fails at first use, and /health can still answer 200.
COPY *.py ./

# The four runtime JSON files are NOT copied -- see note 1 above. They arrive as read-only
# file mounts at /app/<name>.json. Without them the router starts and then fails to serve;
# deploy/README.md has the one-time copy step.

# Non-root. The mounts above are read-only, so this user never needs to write to them.
RUN useradd -m rameau && chown -R rameau:rameau /app
USER rameau

EXPOSE 8080

# Bake the commit so /status reports something real instead of "unknown". Pass
# --build-arg RAMEAU_COMMIT=$(git rev-parse --short HEAD).
ARG RAMEAU_COMMIT=unknown
ENV RAMEAU_COMMIT=${RAMEAU_COMMIT}
# A "-dirty" suffix means the image matches no commit. That is worth catching: an image built
# from an uncommitted tree cannot be reproduced from the repository it claims to come from.
LABEL org.opencontainers.image.revision=${RAMEAU_COMMIT}
LABEL org.opencontainers.image.source=https://github.com/jhpritch-dev/rameau-release

# No HEALTHCHECK, deliberately -- see the trap note above.

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", \
     "--loop", "asyncio"]
