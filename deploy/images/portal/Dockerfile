# Dockerfile -- RAMEAU portal
#
# Build context: services/portal/ in this repository (the portal app root, NOT the repo root).
# Two stages: Vite build, then nginx serving the built bundle with a templated config.
#
# The portal is in scope for v0.1 by John's ruling of 2026-10-27. What is NOT in scope is the
# portal REDESIGN -- this image packages the UI as it stands.
#
# -----------------------------------------------------------------------------------------
# WHICH .jsx IS THE BUILD INPUT -- settled by the entry chain, not by mtime or filename
# -----------------------------------------------------------------------------------------
# The source tree this came from had several same-named files, and the question was settled
# without guessing from dates:
#   index.html declares  <script type="module" src="/src/main.jsx">
#   src/main.jsx  imports  BifrostPortal from "./BifrostPortal.jsx"
# so the build input is src/BifrostPortal.jsx and nothing else. Two other files of that name
# existed elsewhere in the private tree; the Vite entry reaches neither, and neither is in
# this context.
#
# A previously-committed build output (an index-*.js / index-*.css pair) also existed at the
# app root in the original. The Dockerfile.dockerignore beside this file excludes that
# pattern -- and is NAMED for this Dockerfile on purpose, because as plain `.dockerignore` it
# sat outside the build context and Docker read it not at all. If such files reached the BUILD
# stage they would be harmless; if they reached the SERVE stage they would shadow the fresh
# bundle and the portal would silently serve a stale build under a stale asset hash. Do not
# re-include them.
#
# =========================================================================================
# STAGE 1 -- build
# =========================================================================================
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /app

# `npm ci`, not `npm install`. package-lock.json is committed, and ci fails loudly when the
# lock and the manifest disagree, where install would quietly resolve a new dependency tree --
# which is how a lockfile stops meaning anything. Node 20 is kept because that is what the
# original build used: this is a packaging change, not also a toolchain change. If `npm ci`
# rejects the engine (vite 7 / react 19 want >= 20.19 on the 20 line), move to node:22-alpine
# and record that you did. Do not reach for --force.
COPY package.json package-lock.json ./
# The `&& test -x` is not belt-and-braces, it is a MEASURED DEFECT IN npm ITSELF. In the
# npm this image pins (10.8.2), a fatal failure during `npm ci` can print
#     npm error Exit handler never called!
# and still EXIT 0, having installed nothing at all. The build then runs three more
# instructions and dies at `npm run build` with `sh: vite: not found` -- a confusing
# symptom four layers from its cause, and one more case of a step reading green on a real
# failure, which this stack already refuses to accept elsewhere.
#
# Measured on 2026-11-02 under a network fault, not reasoned about. So assert what the
# NEXT instruction actually needs. It names vite because that is what `npm run build`
# runs; if the build tool ever changes, this line changes with it.
RUN npm ci --no-audit --no-fund \
 && test -x node_modules/.bin/vite

COPY index.html vite.config.js ./
COPY public/ ./public/
COPY src/ ./src/
RUN npm run build

# =========================================================================================
# STAGE 2 -- serve
# =========================================================================================
FROM nginx:alpine@sha256:72aeb3308ef6cb3118242fb16389518923916dfa206e32d1a8377acc328b9870

# nginx.conf is copied as a TEMPLATE, not into conf.d, so the nginx image's own
# 20-envsubst-on-templates.sh resolves ${BIFROST_HOST} at container start.
#
# THE FILTER IS LOAD-BEARING. This config carries around 25 nginx runtime variables --
# $host, $uri, $remote_addr and the rest -- and an unfiltered envsubst would substitute every
# one of them with the empty string. The result is a config that STARTS and proxies wrongly,
# which is strictly worse than one that fails to start, because nothing reports it. The
# filter restricts substitution to names beginning BIFROST_.
ENV NGINX_ENVSUBST_FILTER="^BIFROST_"
COPY nginx.conf /etc/nginx/templates/default.conf.template

COPY --from=build /app/dist/ /usr/share/nginx/html/

EXPOSE 80

# No CMD: the nginx image's own entrypoint performs the templating above and its CMD is right.

# -----------------------------------------------------------------------------------------
# HOST COUPLING THAT SURVIVES THIS IMAGE -- the image is portable, the config is not yet
# -----------------------------------------------------------------------------------------
# nginx.conf proxies several routes to `host.docker.internal`, a Docker Desktop name that does
# NOT resolve inside a container on a Linux host. The image builds and starts anyway, nginx
# returns 502 on those routes, and / still serves the SPA -- so the portal looks up and the
# affected panels are simply empty, which is a confusing symptom for a DNS cause.
#
# deploy/compose.yaml therefore passes `extra_hosts: host.docker.internal:host-gateway`, which
# is the documented remedy and is enough for the stack to work. It is not the end state: those
# upstreams should be templated to compose SERVICE names, the way the three LAN routes already
# were, so the config stops naming the host at all. That change belongs to the resolver sweep
# over nginx.conf, not to this Dockerfile -- keeping them apart means the packaging change and
# the endpoint change are reviewable as separate diffs.
#
# Under the no-egress overlay the name still resolves and the route is gone, so those routes
# time out rather than refusing. That is expected; see deploy/compose.no-egress.yaml.
