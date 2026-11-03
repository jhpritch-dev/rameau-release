# Running RAMEAU with Docker Compose

This directory holds the v0.1 deployment: two images, one stack, and an overlay that removes
the stack's route off the machine.

| file | |
|---|---|
| `compose.yaml` | the stack — router and portal |
| `compose.no-egress.yaml` | overlay that makes the network `internal: true` |
| `images/router/` | the router image and its build context list |
| `images/portal/` | the portal image and its build context list |
| `profiles/compose.env.example` | endpoint values for inside the container network, generated from `shared/resolver.py` |
| `config/` | the four `*.example.json` placeholders, and where your own copies go (see step 2) |

## Two things are not ready yet

Stated first, because the stack does not build without them and you should not discover that
by running it.

**1. There is no dependency manifest.** `images/router/Dockerfile` needs
`services/router/requirements.txt`, and no such file has been selected for the release — the
private repository's version is excluded from publication. The build stops on that COPY.
Inventing a pin set is worse than a build that stops, because a reviewer cannot tell which
pins were measured.

**Today it stops EARLIER than that, and the distinction is worth keeping straight.** Neither
build context is assembled, so the build never reaches an instruction in either Dockerfile:
it fails while preparing the context. Measured on 2026-11-03, Docker 29.4.2 / Compose
v5.1.3, from `deploy/`:

```
$ docker compose -f compose.yaml build
unable to prepare context: path ".../services/portal" not found
```

So the COPY failure above is the state you reach once step 4 has landed the source and the
manifest is still absent — it is the *next* failure, not the current one. Until then a reader
who sees "fails on that COPY" and gets a context error has not found a second problem.

**The "131 dependency decisions" figure that used to appear here is withdrawn** — it was
another service's. Reported 2026-11-02: 131 is two `bifrost-kb` files summed, and that
service is not in this release; the router's manifest is five range specifiers. Nothing here
can check that, so no replacement figure is quoted as if it were measured in this
repository.

**What was open here is now closed.** `services/router/` is ruled the release's router
source, and the import closure over that tree is verified in both directions, so the
manifest's content is settled. `docker build` still fails on that COPY because the file is
not here, and that is a **placement** decision belonging to step 4 rather than a gap in
knowledge: committing it alone would leave `services/router/` holding a manifest and none of
the source it describes, which turns CI red. RELEASE-SCOPE.md carries the closure as
evidence, and the one remaining decision -- floors or exact pins -- with it.

**2. The router source is not assembled yet, and the four config examples carry no schema.**
`services/router/` and `services/portal/` are the build contexts this stack expects; the
assembly that fills them has not run.

`config/*.example.json` now exist for all four runtime JSON files, but read one before you
rely on it: **each is a placeholder, not a derived schema.** This repository contains no
sample, loader, test or schema for any of the four, so no field of any of them could be
established — and an invented field would be worse than an acknowledged gap, because the next
reader builds against it. Each file records, in `_`-prefixed prose keys, what the repository
does establish (its mount path, how it is read, that `fleet_config.json` re-reads on mtime)
and what it does not. **Copying them into `config/` will not start the router.** What they do
buy you is that the bind mounts in `compose.yaml` have real files to point at: Docker creates
an empty *directory* at a missing bind source, so a missing file mounts a directory onto
`/app/fleet_config.json` and surfaces as an `IsADirectoryError` a long way from its cause.

Everything below is correct for the point at which those land.

## What you need

- Docker with Compose **v2.24 or newer** — `compose.yaml` uses `env_file: required: false`.
- An **ollama you already run**, reachable from this host. The stack does not ship one, and
  that is deliberate: a container wanting the accelerator would need device passthrough, which
  ties the whole stack to one vendor's driver stack on one kind of host. Inference stays on
  the host; the router gets a URL.

## One-time setup

**1. Endpoints.**

```
cp profiles/compose.env.example profiles/compose.env
```

Then edit it. Every value in it is a **compose service name**, resolved by the container
network's DNS — loopback is wrong there, because inside a container loopback is the container.
Endpoints you are not running **must** be left blank — a blank value counts as unset, and
`shared/resolver.py` records which capability each one unlocks. That was optional until
2026-11-02; the next paragraph is why it is not.

**Blanking them is now load-bearing, where before it was optional.** The example ships
**non-empty** values for six names `compose.yaml` does not define — `ollama`, `embed`, `kb`,
`arbiter`, `broadcaster` and `search` — and `compose.yaml` defines only `router` and `portal`.
So copying the example and starting the stack **without editing** hands the router five
endpoints that resolve to nothing. Measured with `docker compose config` on Docker 29.4.2 /
Compose v5.1.3 against this file as shipped: the router receives `http://embed:11434`,
`http://kb:8090`, `http://arbiter:8082`, `http://broadcaster:8090` and `http://search:8080`,
none of which is a service here.

Until the `env_file:` fix below, those five arrived as the empty string, which
`shared/resolver.py` reads as *unset* — so the wrong values were masked by the wrong
mechanism and the default happened to be the safe one. Making the file reach the router is
correct and also removes that accident, so **blank every endpoint you do not actually run**:
a wrong endpoint is worse than no endpoint, which is the same reason `RAMEAU_OLLAMA_URL`
ships with no default at all.

This file is the **only** place the five optional endpoints are read from — `RAMEAU_EMBED_URL`,
`RAMEAU_ARBITER_URL`, `RAMEAU_KB_URL`, `RAMEAU_BROADCASTER_URL`, `RAMEAU_SEARCH_URL`. The shell
and `deploy/.env` do not reach the router for those. (Until 2026-11-02 the reverse was true and
this file reached nothing: the router re-declared each one under `environment:`, which outranks
`env_file:`, so they arrived as the empty string. `compose.yaml` now omits them, which is the
only form that lets this file through, and CI asserts it with a sentinel.)

**`RAMEAU_OLLAMA_URL` is the exception, and putting it in this file will not work.** It has no
default and the stack refuses to start without it — but it is declared `${RAMEAU_OLLAMA_URL:?}`,
and `:?` is resolved at *interpolation* time, before any `env_file` is read. So it goes in your
shell or in `deploy/.env`:

```
printf 'RAMEAU_OLLAMA_URL=http://host.docker.internal:11434\n' > .env   # .gitignore covers it
```

On Docker Desktop, and on Linux with the stack's `extra_hosts`,
`http://host.docker.internal:11434` reaches an ollama on the host. The three router URLs in the
example file are also ignored: `compose.yaml` sets those itself, on purpose.

**`BIFROST_HOST` is the same exception, and it is the one variable this section did not name
until 2026-11-03.** It is the portal's nginx template variable, and `compose.yaml` declares it
as `${BIFROST_HOST:-host.docker.internal}` under the portal's `environment:` — which outranks
`env_file:`, exactly as it did for the five endpoints above. So putting it in
`profiles/compose.env` does nothing, silently. Measured: with that file setting
`BIFROST_HOST=file-wins-bifrost.example.com`, the portal still resolved
`BIFROST_HOST=host.docker.internal`.

You almost certainly do not need to set it — the default is right for this stack, because
`compose.yaml` passes `extra_hosts: host.docker.internal:host-gateway`. If you do need to, it
goes in your shell or in `deploy/.env`, beside `RAMEAU_OLLAMA_URL`.

One more thing that measurement showed, recorded because it reads as a mistake and is not one
worth acting on blind: both services share one `env_file:` anchor, so the portal also receives
the six `RAMEAU_*` endpoint variables and the router also receives whatever `BIFROST_*` the
file sets. Neither is read — nginx substitutes only names matching `^BIFROST_`, and the router
resolves only `RAMEAU_*` — so nothing misbehaves today. Splitting the anchor is a deploy-surface
change and is left to whoever owns that decision; RELEASE-SCOPE.md carries it as open.

**2. Runtime config.**

```
cd config
for f in *.example.json; do cp "$f" "${f%.example.json}.json"; done
```

The four are `fleet_config.json`, `capability_registry.json`, `capability_profiles.json` and
`bifrost_shell_policy.json`. Each is read as `Path(__file__).parent / "<name>.json"`, so
`compose.yaml` mounts them one file at a time to exact paths rather than mounting a directory
— a directory mounted onto `/app` would shadow the application code.

The copies you just made are **placeholders with no schema in them** and will not start the
router — see "Two things are not ready yet" above. `.gitignore` excludes
`config/*.json` while keeping `config/*.example.json` tracked, because your copies describe
your fleet and the examples describe nothing.

They are mounted read-only, so the container cannot drift from what you edited.
`fleet_config.json` re-reads on mtime change on every request, so editing it re-tiers a
**running** router with no restart.

## Bring it up

```
docker compose up --build
```

| what | where | note |
|---|---|---|
| router | `127.0.0.1:18080` | `RAMEAU_ROUTER_PUBLISH` to change |
| portal | `127.0.0.1:13110` | `RAMEAU_PORTAL_PUBLISH` to change |

Both bind **127.0.0.1 explicitly**. A bare `18080:8080` would bind `0.0.0.0` and publish a
router that proxies to a local model to every machine on the network the host sits on. If you
want that port exposed, override the variable, so that the exposure is a decision someone
wrote down.

Inside the network the router is `http://router:8080`. That is the port the container listens
on, and it is **not** the port the private deployment published — an in-container URL built
from a host-side port resolves to nothing. That mistake was in `profiles/compose.env.example`
until **2026-10-27**: six wrong values across five endpoints — `router:8089` three times,
`kb:8091`, `broadcaster:8092`, `search:8888` — plus a health path, `/router/status`, that
belongs to the portal's nginx rather than to the router. `.env.example` carried three of them
too. It is fixed at the source: `shared/resolver.py` now records what each unit listens on
separately from its host-side example, every one of those numbers cites the artifact it was
read out of, and `shared/test_resolver.py` asserts the registry against
`images/router/Dockerfile`, against `compose.yaml` and against both generated files.

Read `PROVENANCE` in `shared/resolver.py` before trusting a listen port. Only the router's
image ships in this repo, so only the router's port can be checked against the thing that
actually listens; for the rest the citation names an artifact in the private archive and the
test can only catch a one-sided edit. `embed` is weaker still and says so — there is no
container artifact for it at all.

Neither example file should be hand-edited — regenerate both:

```
python shared/resolver.py --write-examples
```

That writes the files itself rather than printing for a shell to redirect. The redirect form
still exists (`--env-example`, `--compose-example`) but on Windows it produces CRLF whatever
shell runs it — Python's text-mode stdout does the translation, not the shell — so the content
comes out right and the bytes wrong, which the test comparing file to generator reports as a
failure with no apparent cause.

## No egress

```
docker compose -f compose.yaml -f compose.no-egress.yaml up --build
```

This removes the network's gateway: no internet, no LAN, no DNS beyond Docker's resolver,
enforced by the network stack rather than by anything the application promises.

Because inference lives on the host, **generation will fail under this overlay** — the host
gateway is exactly what has been severed. That is the useful result: it demonstrates the
router has no route out. If you need generation to work as well, put a backend on the network
yourself and point `RAMEAU_OLLAMA_URL` at it by service name. What this overlay must not
become is a quiet re-opening of egress because generation failed.

Read the header of `compose.no-egress.yaml` before relying on it. `internal: true` constrains
egress from this network only; it does not isolate the services from each other, and it is
not the equivalent of the private deployment's NetworkPolicy objects.

## Accepting a build: do not stop at a health check

**`/status` returning 200 is not sufficient**, and the stack defines no healthcheck for this
reason rather than by omission.

`bifrost_delegate.py` is read off disk with `ast` and is imported by nothing. A build context
computed from an import closure therefore drops it — and the tool loop then refuses **every
delegated leaf**, fail-closed, while `/status` still answers 200. A healthcheck on health
would read green on exactly that failure, and the container would be declared ready.

So the acceptance test has to **exercise one delegated leaf**. Anything less tests that the
process started.

### The procedure, step by step

Written out rather than left as prose, because this is the test that decides whether the
dependency manifest is right: **a missing requirement fails at import time inside the
container, after a green build.** CI cannot host this — it needs the four runtime JSON files
as real files and an ollama on the host, and a runner has neither — so it is run by hand at
step 5 and the steps belong here.

**1. Build with no cache**, so a stale wheel cannot make a broken manifest look installable.
The Dockerfile already passes `--no-cache-dir` to pip; `--no-cache` is the builder's half of
the same precaution.

```
cd deploy
printf 'RAMEAU_OLLAMA_URL=http://host.docker.internal:11434\n' > .env   # .gitignore covers it
docker compose -f compose.yaml build --no-cache
```

**2. Put the four runtime files in place.** They are bind-mounted read-only to exact paths,
so they must exist as *files* — Docker silently creates a *directory* at a missing bind
source, and the container then fails with `IsADirectoryError` a long way from its cause.

```
cd config
for f in *.example.json; do cp "$f" "${f%.example.json}.json"; done
```

As shipped these are placeholders that carry no schema, so **step 5 cannot pass with them**;
they come from the assembled source. Copying them is still worth doing first, because it
removes the directory-mount failure from the list of things that can be going wrong.

**3. Start the stack and confirm the process is up — without believing it.**

```
docker compose -f compose.yaml up -d
curl -fsS http://127.0.0.1:18080/livez          # liveness, dependency-free by design
curl -sS  http://127.0.0.1:18080/health | ...   # READ THE BODY: 200 either way
```

`/livez` answering proves the image imports and serves. It proves nothing about the manifest
beyond the modules reached during startup, which is the trap: an import that only happens on
a request path has not run yet.

**4. Exercise one delegated leaf.** This is the step that is the test.

**The specific request is deliberately not written here, because this repository cannot
supply it.** The router source is not assembled, so the tool-loop surface — which call
delegates, what a leaf's payload looks like, what a refusal looks like against a success — is
not knowable from inside this tree. Inventing a plausible request would be worse than leaving
the gap: it would most likely exercise nothing, pass, and be recorded as acceptance.

> **OWED BY: the private side, and accepted there on 2026-11-02.** This is a handoff with a
> named owner, not an omission. It can only be written by whoever can read the unassembled
> router source, which is not this repository and will not be until step 4. If you are
> reading this at step 4 and the invocation below is still missing, it is outstanding from
> that owner — do not substitute one, for the reason in the paragraph above.

What is owed is one concrete invocation, with:

- the request that causes exactly one leaf to be delegated;
- the response that means it ran;
- the response that means it was **refused fail-closed** — which is the failure this whole
  test exists to catch, because `/status` answers 200 throughout it.

Until that lands, treat step 5 as unverifiable rather than as passing. A green build, a live
`/livez` and a 200 from `/health` are all consistent with a router that refuses every
delegated leaf.

**5. If it fails, read it in this order.** The failure modes are layered and the outer ones
mask the inner:

| symptom | first thing to check |
|---|---|
| build stops at a `COPY` | the context is assembled and `Dockerfile.dockerignore` admits what the `COPY` names |
| build green, container exits immediately | a missing requirement — import-time failure, see the manifest obligations in RELEASE-SCOPE.md |
| container up, `/livez` fine, leaf refused | `bifrost_delegate.py` absent from the context; it is imported by nothing and read with `ast` |
| portal crash-loops | an unresolvable literal upstream — see below |

Two more symptoms worth recognising, both of which look like something other than their cause:

- **The portal container crash-loops and serves nothing.** `nginx.conf` proxies **five**
  routes to `host.docker.internal` -- literal upstreams, which nginx resolves when it
  *parses* its config, so a name that does not resolve is fatal at startup:
  `[emerg] host not found in upstream`. `compose.yaml` passes
  `extra_hosts: host.docker.internal:host-gateway`, so inside this stack the name resolves
  and none of this bites; **it applies to the portal image run outside the stack**, where
  you need the equivalent `--add-host`.

  **This entry said "the portal loads and its panels are empty" until 2026-11-02.** That is
  what the file's *other* three upstreams do -- variable, behind a single `resolver`
  directive, resolved per request, so they 502 while `/` keeps serving. It is the wrong
  symptom for the five literal ones, and the cause was right all along. The file is not in
  this repository; RELEASE-SCOPE.md carries the full split and records that the release
  derives from the live variant.
- **Generation times out rather than refusing.** That is the no-egress overlay: the name still
  resolves and the route is gone, so there is no gateway to send a reset.
