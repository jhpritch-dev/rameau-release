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
private repository's version is excluded from publication. `docker build` fails on that COPY.
Inventing a pin set was the alternative, and 131 dependency decisions made by guesswork, with
no way for a reviewer to tell which were measured, is worse than a build that stops.

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
Endpoints you are not running may be left blank; a blank value counts as unset, and
`shared/resolver.py` records which capability each one unlocks.

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

Two more symptoms worth recognising, both of which look like something other than their cause:

- **The portal loads and its panels are empty.** `nginx.conf` proxies several routes to
  `host.docker.internal`, which does not resolve inside a container on a Linux host.
  `compose.yaml` passes `extra_hosts: host.docker.internal:host-gateway` to fix it; if you
  run the portal image outside this stack, you need the equivalent `--add-host`.
- **Generation times out rather than refusing.** That is the no-egress overlay: the name still
  resolves and the route is gone, so there is no gateway to send a reset.
