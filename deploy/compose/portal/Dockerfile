# Dockerfile -- bifrost-portal / switchboard        NEWLY AUTHORED (no Dockerfile existed)
#
# Unit:          Portal / switchboard
# Source repo:   bifrost-platform @ 9c9a0d5, subtree bifrost-portal/
# Build context: bifrost-platform/bifrost-portal/   (NOT the platform repo root)
# Replaces:      bifrost-platform/scripts/build-portal.ps1  +  a bare `nginx:alpine` with two
#                Windows bind mounts (L:/bifrost-portal -> html, L:/bifrost-portal-conf ->
#                conf.d). After this image exists, NEITHER bind mount is needed.
#
# ===========================================================================
# "WHICH .jsx IS CURRENT" -- SETTLED BY THE COMMITTED TREE, not by mtime or filename
# ===========================================================================
# The assessment left this UNVERIFIED: 14 `.bak-*` siblings, `_fixed`, `_v2`, `.block3.bak`,
# three build scripts naming three different drive letters (L:, P:, X:). In the committed
# capture it resolves cleanly, and the resolution does not use mtime or a filename guess:
#
#   a) bifrost-portal/.gitignore ignores  node_modules, dist, *.bak, *.bak-*, *-bak,
#      *_fixed.jsx, *_v2.jsx, patch-*.py, patch_*.py, *.log.
#      So NONE of the .bak/_fixed/_v2 variants is committed. The ambiguity was an artefact
#      of the un-gitignored RAMEAU working copy, not of the repo.
#   b) The Vite entry chain is explicit and single-valued:
#         bifrost-portal/index.html:11   <script type="module" src="/src/main.jsx">
#         bifrost-portal/src/main.jsx:4  import BifrostPortal from "./BifrostPortal.jsx"
#      => the build input is bifrost-portal/src/BifrostPortal.jsx, 243,617 B.
#   c) The two other committed BifrostPortal.jsx are NOT in this context and are not the
#      build input: bifrost-platform/BifrostPortal.jsx (153,671 B, repo root) and
#      bifrost-router/BifrostPortal.jsx (134,939 B, a different repo). Both are smaller
#      snapshots outside the app; the Vite entry reaches neither.
#   d) Corroboration, not evidence: bifrost-platform's HEAD subject at capture is
#      "docs: 35 tiers, withdraw the 2_qwen_coder defect, correct the portal source tree".
#
# NOTE on bifrost-portal/assets/ : index-CtA2smOU.js (592,990 B) + index-DQ3P1g1z.css are a
# PREVIOUS build output committed at the app root (while .gitignore ignores `dist`). Vite
# writes to dist/, so these are stale and are NOT a build input. The .dockerignore beside
# this file excludes them -- if they reached the build stage they would be harmless, but if
# they ever reached the SERVE stage they would shadow the fresh bundle and the portal would
# silently serve an old build with a stale asset hash. That is the failure the
# cache-control block in nginx.conf exists to prevent, so do not re-include them.
#
# ===========================================================================
# STAGE 1 -- build
# ===========================================================================
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /app

# package-lock.json IS committed, so use the reproducible install. `npm ci` fails loudly if
# the lock and the manifest disagree; `npm install` would silently resolve a new tree.
# build-portal.ps1 used `npm install --silent`, which is how a lockfile stops meaning
# anything. Node 20 is kept deliberately: it is what build-portal.ps1 used, so this is a
# packaging change and not also a toolchain change. vite 7 / react 19 want >=20.19 on the
# 20 line -- if `npm ci` rejects the engine, move to node:22-alpine and say so, do not
# add --force.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY index.html vite.config.js eslint.config.js ./
COPY public/ ./public/
COPY src/ ./src/
RUN npm run build

# ===========================================================================
# STAGE 2 -- serve
# ===========================================================================
FROM nginx:alpine@sha256:72aeb3308ef6cb3118242fb16389518923916dfa206e32d1a8377acc328b9870

# The nginx conf IS in the capture: bifrost-platform/bifrost-portal/nginx.conf.
# The assessment recorded it as "not in tree" and warned that "No conf found is not no conf
# needed" -- correct warning, but the fresh capture refutes the premise. It is baked here
# instead of bind-mounted from L:\bifrost-portal-conf.
# 2026-10-15 (P5): copied as a TEMPLATE, not straight into conf.d, so the nginx image's
# 20-envsubst-on-templates.sh resolves ${BIFROST_HOST} at container start. The FILTER is
# not optional: this config carries 25 nginx runtime variables ($host, $uri, $remote_addr,
# ...) and an unfiltered envsubst would blank every one of them, yielding a config that
# starts and proxies WRONGLY -- worse than the boot failure this replaces.
ENV NGINX_ENVSUBST_FILTER="^BIFROST_"
COPY nginx.conf /etc/nginx/templates/default.conf.template

# The built bundle, baked instead of bind-mounted from L:\bifrost-portal.
COPY --from=build /app/dist/ /usr/share/nginx/html/

EXPOSE 80

# No CMD: nginx:alpine's own entrypoint/CMD is correct and its conf.d templating is what
# picks up the file above.

# ===========================================================================
# HOST COUPLING THAT SURVIVES THIS IMAGE -- the image is portable, the conf is not
# ===========================================================================
# nginx.conf proxies to SEVEN upstreams, and five of them are host-coupled:
#   host.docker.internal:8089  /router/        Docker-Desktop-only DNS name
#   host.docker.internal:8091  /kb/            "
#   host.docker.internal:8092  /broadcaster/   " (and the conf's own comment says this
#                                               currently 504s: k3d serverlb does not
#                                               expose :8092)
#   host.docker.internal:8082  /arbiter/       " (same, 504s today)
#   host.docker.internal:9090  /prom/          "
#   192.168.x.33:8092          /retriever/     LAN literal (Bifrost)
#   192.168.x.33:8093          /dispatcher/    LAN literal (Bifrost)
#   192.168.x.33:8080          /webhook/       LAN literal (Bifrost)
# host.docker.internal does NOT resolve inside a container on a Linux host without
# `--add-host host.docker.internal:host-gateway`. The image will build and start anyway and
# nginx will return 502 on those five routes while / still serves the SPA -- i.e. the portal
# looks up and every panel is empty. Templating these to env is the right fix and belongs to
# the volume/endpoint packet, not here; this Dockerfile deliberately bakes the conf VERBATIM
# so the packaging change and the endpoint change are not entangled in one diff.
# The build and dev-loop descriptor must therefore pass --add-host (Compose: extra_hosts).
