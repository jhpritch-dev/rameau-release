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
# THE DEPENDENCY MANIFEST -- RESOLVED 2026-11-13, and what it still does not settle
# -----------------------------------------------------------------------------------------
# This block said "THIS IMAGE CANNOT BUILD YET -- requirements.txt is not in the release"
# until 2026-11-13. It builds: services/router/requirements.txt exists and the images job is
# green on the runner. The reasoning is kept rather than deleted because the gap it describes
# only half closed.
#
# What it was right about: the private repository's manifest is dispositioned
# EXCLUDE_INTERNAL, so none of its pins could be copied, and a missing file stopping the build
# was the honest state of that.
#
# What is still open: the manifest that now exists is five RANGE specifiers, not pins, so two
# builds of this one commit on different days can still be different images -- the exact
# objection this block raised against installing unpinned. Only numpy is pinned, and only
# because the repository's own requirements.txt had already measured that pin for
# routing_cache.py on CPython 3.11, which is this base image. The other five remain ranges
# nobody has measured a floor for. Do not read a green build as a reproducible one.
#
# This comment also quoted "131 dependency decisions" until 2026-11-02. That figure was
# another service's -- two bifrost-kb files summed, for a service not in this release.
#
# -----------------------------------------------------------------------------------------
# THE TRAP THIS CONTEXT INTRODUCES -- do not accept this image on /health alone
# -----------------------------------------------------------------------------------------
# bifrost_delegate.py is read off disk with `ast` and is imported by nothing. A build context
# computed from an import closure therefore omits it, and the tool loop then refuses EVERY
# delegated leaf, fail-closed, while /health still answers 200. The acceptance test has to
# exercise one delegated leaf. This is also why compose.yaml defines no healthcheck: a
# healthcheck on /health would read green on precisely this failure.
#
# SUSPENDED 2026-11-02. The router source ruled into scope contains no bifrost_delegate.py
# and no tool loop, so the trap above cannot occur in what this image will actually build
# and the acceptance step it prescribes cannot be run. Kept rather than deleted: the
# reasoning is sound about the artefact it was written for, and the no-healthcheck decision
# does not depend on it. See RELEASE-SCOPE.md.

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

# ---- layer 3: THE SKILL REGISTRY, CUT 2026-11-13 ----------------------------------------
# `COPY skills/ /app/skills/` used to be here. RULED BY JOHN: cut the directory.
#
# WHY, measured. The sixteen Markdown files it copied were the private deployment's operator
# playbook, and nothing in this image read them -- `services/router/*.py` contains no
# reference to `skills/` at all, so they were inert payload. They described named machines and
# their LAN addresses, a Tailscale funnel, a k3d control plane and a session codename, and
# `skills/router/references/cascade-tables.md` contradicted the `config.py` beside it in eight
# places. One file carried BOTH of the tree's address-masking styles -- one replaces the THIRD
# octet and shows the fourth, the other replaces the FOURTH and shows the third -- so its own
# lines reconstructed between them what each was individually hiding. The private-literal
# scanner passed them only because a masked form is not a valid address literal, which is the
# gap its own header admits it cannot close.
#
# NEITHER FORM IS WRITTEN OUT HERE, deliberately. An earlier draft of this comment quoted both
# to explain the problem and thereby became the only file in the tree carrying the second one
# -- reproducing the defect in the course of describing it. Measured after the rewrite: no
# tracked file outside harness/ carries both styles.
#
# THE OBLIGATION IS INVERTED, NOT DELETED. Preflight B2 used to assert `skills/` was present,
# non-empty and Markdown-only, on the grounds that an empty one would ship a router with no
# skills. B8 now asserts the opposite -- that the directory is absent AND that no COPY here
# names it -- because a cut only holds if something notices it coming back. Re-adding the
# directory by reflex is exactly the failure this image should not permit twice.
#
# Nothing replaces it. A skills registry that an operator wants is configuration, not image
# payload, and should arrive on a mount with the four runtime JSON files.

# ---- layer 3b: the endpoint registry ----------------------------------------------------
# `shared/resolver.py`, byte-identical to the copy at the repository root -- a CI step asserts
# that, so this cannot drift into a second registry. It needs its own COPY because the
# wildcard in layer 4 does not descend into directories, and it must keep the `shared/`
# package path because that is how the code imports it in both places
# (`from shared.resolver import resolve`); flattening it to `resolver.py` here would make the
# image's import line differ from the repository's for no gain.
#
# WHY THE ROUTER NEEDS IT AT ALL: routing_cache_wiring.py resolves the embedding endpoint
# through the registry rather than carrying its own copy of the variable names, so an operator
# who sets RAMEAU_EMBED_URL configures this call site along with the others, and an
# unconfigured one gets a ConfigError instead of a guess at somebody's LAN.
COPY shared/ /app/shared/

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
