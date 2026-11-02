# RAMEAU

A tier-routing layer for local language models. RAMEAU puts an OpenAI-compatible endpoint
in front of several locally hosted models of different sizes, classifies each incoming
request, and routes it to the smallest tier that can answer it -- falling through a cascade
when a tier is unavailable.

**Status: pre-release. This repository is scaffolding.** The source tree has not landed yet.
See [RELEASE-SCOPE.md](RELEASE-SCOPE.md) for what v0.1 will and will not contain, and why
the repository is being assembled in this order.

**Can you run it today? No.** The Compose stack is written and parses, but neither image can
be built: both build contexts (`services/router/`, `services/portal/`) are directories that
do not exist in this repository yet. `docker compose build` stops with
`unable to prepare context: path ".../services/router" not found`. What is runnable today is
one module and its tests. [How to deploy the stack](#how-to-deploy-the-stack) documents the
real path end to end and marks exactly which steps work now.

Licensed under the [Apache License 2.0](LICENSE).

## Why this exists

Most routing layers assume a cloud provider and optimise for cost per token. RAMEAU assumes
the opposite: the models are already running on hardware you own, the marginal cost of a
token is electricity, and the thing worth optimising is **latency against answer quality**.
A request that a 4B model answers correctly should not wait on a 30B model.

That inverts several design choices:

- **Classification happens before generation**, not as a model call. A routing decision that
  costs a model call has already spent what it was trying to save.
- **A cache sits in front of the classifier**, because in practice a large share of traffic
  is near-duplicate.
- **Tiers degrade rather than fail.** When a tier is down the cascade continues to the next
  one; the caller gets a slower answer, not an error.
- **Empty content is a normal outcome, not a failure.** A reasoning model can exhaust its
  token budget before it produces visible output, and a correctly behaving tool-calling
  model returns empty content with its payload in `tool_calls`. A router that treats either
  as an error produces retry storms.

That last point is the one most likely to bite an integrator, which is why it is in the
README rather than buried in a troubleshooting page.

## Shape

```
client (OpenAI-compatible)
        |
        v
   +----------+     cache hit -> served
   |  router  |---> classify ---> tier selection ---> cascade
   +----------+
        |
        v
  local model hosts (one or more), each serving one or more tiers
```

Everything above the model hosts is in scope for this release. The model hosts themselves
are not: RAMEAU talks to an existing OpenAI-compatible server and does not manage weights,
GPUs or inference runtimes.

## What this is not

- **Not a model server.** It routes to one; it does not run one.
- **Not a cloud gateway.** There is no cloud provider in the default path.
- **Not a benchmark harness.** Throughput figures belong to the hardware, and a routing
  layer that published them would be comparing machines rather than configurations.
- **Not a drop-in replacement** for a hosted API's full surface. See RELEASE-SCOPE.md.

## What is here and working

File-level, because "working" is a claim and a file is checkable.

**`shared/resolver.py` -- the one real module (654 lines, stdlib only).** Every service
address the router talks to is resolved here, from the environment, and nowhere else. Three
properties, all of them behaviours rather than intentions:

- **No fallback to a literal address, ever.** An unset endpoint raises `ConfigError` at the
  point of resolution, naming the variable, what breaks without it, and a well-formed
  example. There is no default URL anywhere in the registry.
- **A redacted placeholder is refused loudly.** The private archive's addresses had been
  masked in place, which left call sites defaulting to values like `http://192.168.x.4:8089`
  -- not a leak, and not a working value either. Returned to a caller, such a value fails
  several layers down as a DNS error, far from the thing the operator actually got wrong.
  The resolver refuses it with the host quoted and the word "redacted" in the message. The
  rule is shape-based (four fields, at least one real octet, every non-octet field at most
  two characters) and its blind spots are written down in the docstring rather than left to
  be discovered -- `1.2.3.foo` and `x.x.x.x` pass, and the module says why.
- **An empty value counts as unset.** Compose's `env_file` injects every key in the file, so
  a blank-valued example arrives as set-but-empty for all nine endpoints. Treating that as
  an error would hard-fail a core-only deployment on exactly the endpoints whose
  `required_for` tags exist so they can be left out.

It also carries the endpoint registry itself: nine endpoints, each with its canonical
variable, its compatibility aliases, the capability it unlocks, a host-side example, the
port the service **listens on**, and a `PROVENANCE` entry naming the artifact that number
was read out of. `listens_on` is deliberately not the same field as the port inside
`example`: the first is what a process binds, the second is a host-side value that may go
through a published port. Conflating them is what put six wrong ports in the generated
compose profile before 2026-10-27. Of the nine endpoints, three (`router`, `router_chat`,
`router_health`) are anchored to an artifact that ships in this repository and can therefore
be checked here; the other six cite an artifact in the private archive, and `PROVENANCE`
says plainly that this is weaker and what it cannot catch.

**`shared/test_resolver.py` -- 87 tests, all passing.** Measured on 2026-11-02 with
Python 3.11.9 and pytest 9.0.2:

```
python -m pytest shared/test_resolver.py --tb=line -q
```

They cover the resolution rules above, every shape of redaction the archive used, the
permissive side (a legitimate all-numeric DNS label such as `10.infra.example.com` must not
be called a mask), the error-message contents, and the registry's own invariants -- that no
example is a private address, that no canonical variable name carries a machine or
accelerator name, that a declared host/listen port divergence actually diverges, and that
the registry agrees with `deploy/images/router/Dockerfile` and `deploy/compose.yaml` on the
router's port.

**The two generated example env files.** `.env.example` and
`deploy/profiles/compose.env.example` are both produced by `shared/resolver.py --write-examples`.
There are deliberately two, and one file could not serve both cases: `.env.example` carries
loopback values, for running the router directly on a host, while
`deploy/profiles/compose.env.example` addresses services by **name**, for running inside a
container network -- where loopback is the container itself, so a host-oriented file points
every endpoint at nothing. CI regenerates both and diffs them against what is checked in, so
"generated from the registry" is a gate rather than a header comment.

**`.github/workflows/ci.yml` -- the CI gate.** Two jobs. The secret scan runs gitleaks over
the working tree *and* the full history, and before trusting a clean report it plants a
generated high-entropy AWS-shaped key in a scratch repository outside the workspace and
requires gitleaks to flag it -- because a scan that cannot be shown to fire is not evidence.
The canary is generated rather than written literally for two reasons: a literal key would
itself be a finding in the workflow file, and AWS's documented example key is allowlisted by
gitleaks by default, which is how the first version of that control failed. The build job
byte-compiles, runs the tests, regenerates and diffs both env examples, asserts the
in-network profile contains no loopback, and validates both compose files by path -- with a
control that asserts the stack still **refuses** to resolve when `RAMEAU_OLLAMA_URL` is
unset. Every build step is written to pass on an empty tree and to start doing real work the
moment source appears, so the gate never has to be switched on later.

**`tools/git-hooks/pre-push` -- the fail-closed local secret gate.** GitHub's push
protection is unavailable while this repository is private on the current plan (secret
scanning returns 422, rulesets 403), and CI only runs after a push has landed, by which
point rotation rather than deletion is the remedy. The hook restores the
block-before-it-leaves property locally. It runs the same generated-canary control, scans
local history as the gate, and scans the working tree as an **advisory** pass -- advisory
because `--no-git` also reads untracked and gitignored files, so a local `.env` would
otherwise refuse every push, and a gate people turn off protects nothing. Install it with
`git config core.hooksPath tools/git-hooks`. With gitleaks absent it refuses the push rather
than waving it through.

**The rest of the scaffolding.** Apache-2.0 `LICENSE`; `SECURITY.md`, which routes reports
through GitHub private vulnerability reporting and states a realistic one-week
acknowledgement rather than an aspirational one; a `.gitignore` written as the second of
three credential layers; `.gitattributes` normalising the tree to LF because it is assembled
on Windows and runs on Linux; `RELEASE-SCOPE.md` as the scope contract; and
`deploy/`, which is documentation and configuration that is correct for the point at which
the source lands -- see the deployment section below for what that means today.

## What needs development

**The repository is at step 1 of 6** of the assembly order in RELEASE-SCOPE.md. Step 1,
scaffolding, is done. The remaining five:

2. **The allowlist.** An explicit, mechanically generated list of which paths from the
   private archive enter this tree. The census of 2026-10-27 selected **282 of 7,009 source
   paths, 3,757,558 bytes**: 200 straight byte copies, 68 needing the resolver sweep, 7
   derived as `.example` files, 7 needing documentation scrubbing. Twenty paths remain
   undecided and are triaged separately.
3. **The credential scrub.** Removal verified by more than one independent scanner, over
   both the working tree and the full history of the source archive. **This step gates
   everything after it: step 4 does not begin until step 3 reads clean.** The reason is in
   RELEASE-SCOPE.md and is not negotiable -- a credential committed to a private repository
   has still left the machine it was on, and no later rewrite un-sends it.
4. **The source lands,** assembled from the allowlist into a history that begins here.
5. **Containerisation.** The Compose stack and the offline profile. The files in `deploy/`
   are written against the source that has not arrived; this step is what makes them build.
6. **Hardening and release.** Documentation, an independent final scan, then the tag.

### Four open scope questions

RELEASE-SCOPE.md records these as unresolved, and they are listed here rather than left in
one document because they decide what the stack contains. John's ruling of 2026-10-27
covered two of six families -- the portal and autopilot both stay in -- and did not extend
to the other four:

| open item | the problem |
|---|---|
| `igor_tool_loop.py` | selected by the census; named by neither the in-scope nor the cut table |
| `bifrost_delegate.py` | same gap, and the module the acceptance test exists for (see below) |
| the decomposer (4 paths) | same gap |
| `docker/shell-mcp/` (6 paths) | **a live contradiction**: the cut list names "the agent sandbox" by name, and six shell-mcp paths are selected anyway |

A gap and a contradiction are different defects. A contradiction announces itself -- two
parts of one document disagree and a reader can cite either. A gap does not, which makes it
the worse of the two.

### A required `NOTICE` file

Nine bundled `.woff2` faces (Instrument Sans, Instrument Serif, JetBrains Mono) and their
three licence texts are in the release set under **SIL OFL 1.1**. That is compatible with
Apache-2.0 but is **not** Apache-2.0, so the third-party attribution has to be stated rather
than implied by the root `LICENSE`. The file does not exist yet because the fonts have not
landed; it goes in with them.

### Also outstanding

- **No dependency manifest.** `deploy/images/router/Dockerfile` needs
  `services/router/requirements.txt`, and the private repository's version is excluded from
  publication. Inventing a pin set would be 131 dependency decisions made by guesswork, with
  no way for a reviewer to tell which were measured; installing unpinned makes two builds of
  one commit different images. A missing file that stops the build is the honest state of
  that gap.
- **The four runtime JSON files** (`fleet_config.json`, `capability_registry.json`,
  `capability_profiles.json`, `bifrost_shell_policy.json`) are dispositioned as `.example`
  derivations and have not been derived.
- **`docker/.env.example` must be read line by line before it is copied.** It was classified
  into the release set on its filename alone, and a `.example` suffix is a convention rather
  than a guarantee.
- **Every file in the planned `docs/` slot is new writing.** The archive's entire `docs/`
  tree is excluded as internal, so no documentation carries over.

## How to deploy the stack

This section documents the real intended path, taken from `deploy/compose.yaml`,
`deploy/compose.no-egress.yaml` and the two Dockerfiles as they stand. Read it with
[`deploy/README.md`](deploy/README.md), which is the longer operator walkthrough.
**[What works today](#what-works-today-and-what-is-blocked) is the last subsection, and a
reader in a hurry should read it first.**

### What the stack defines

Two services on one bridge network named `rameau`.

| service | what it is | container port | published by default |
|---|---|---|---|
| `router` | the OpenAI-compatible endpoint, the pre-generation classifier, tier selection, the cascade and the request cache | 8080 | `127.0.0.1:18080` |
| `portal` | the existing web UI, as it stands -- nginx serving a Vite bundle | 80 | `127.0.0.1:13110` |

Override the published addresses with `RAMEAU_ROUTER_PUBLISH` and `RAMEAU_PORTAL_PUBLISH`.
Both defaults bind **127.0.0.1 explicitly**: a bare `18080:8080` would bind `0.0.0.0` and
publish a router that proxies to a local model to every machine on the host's network. If
you want a port exposed, override the variable, so that the exposure is something somebody
wrote down.

Inside the network the router is `http://router:8080`. That is the port the container
listens on and it is **not** the port the private deployment published -- an in-container URL
built from a host-side port resolves to nothing.

**There is no `ollama` service, and that is not an omission.** A container that wanted the
accelerator would need device passthrough, which ties the stack to one vendor's driver stack
on one kind of host. Inference stays on the host and the router gets a URL. `arbiter`, `kb`,
`broadcaster` and `search` are absent for a different reason: none of their images is part of
v0.1, so defining them here would describe a stack this repository cannot build. They are
URLs if you have them and blank if you do not.

The `portal` service declares `depends_on: router`, which is **start ordering only** --
Compose's plain `depends_on` waits for the container to start, not for the application to be
ready. The portal tolerates a router that is not up: those routes 502 until it is.

Both services get `extra_hosts: host.docker.internal:host-gateway`. Without it, that name
does not resolve inside a container on a Linux host, and the symptom is misleading in both
services: the router starts and every generation request fails, and the portal serves its
SPA while every panel is empty.

### What you need

- Docker with Compose **v2.24 or newer** -- `compose.yaml` uses `env_file: required: false`.
- An **ollama you already run**, reachable from this host.
- The four runtime JSON files in `deploy/config/` (see below). They have not been derived yet.

### Environment

Nine endpoints, all defined in `shared/resolver.py`, which is the only place they are
resolved. The names below are read out of that registry and out of the two generated example
files -- none is invented here.

| registry name | variable | capability | listens on | loopback example |
|---|---|---|---|---|
| `router` | `RAMEAU_ROUTER_URL` | core -- the router's own base, for clients and self-checks | 8080 | `http://127.0.0.1:8080` |
| `router_chat` | `RAMEAU_ROUTER_CHAT_URL` | core -- the full chat-completions URL | 8080 | `http://127.0.0.1:8080/v1/chat/completions` |
| `router_health` | `RAMEAU_ROUTER_HEALTH_URL` | core -- readiness probe target | 8080 | `http://127.0.0.1:8080/health` |
| `ollama` | `RAMEAU_OLLAMA_URL` | core -- the local inference backend | 11434 | `http://127.0.0.1:11434` |
| `embed` | `RAMEAU_EMBED_URL` | embeddings | 11434 | `http://127.0.0.1:11436` |
| `kb` | `RAMEAU_KB_URL` | retrieval | 8090 | `http://127.0.0.1:8091` |
| `arbiter` | `RAMEAU_ARBITER_URL` | arbitration | 8082 | `http://127.0.0.1:8082` |
| `broadcaster` | `RAMEAU_BROADCASTER_URL` | observability | 8090 | `http://127.0.0.1:8092` |
| `search` | `RAMEAU_SEARCH_URL` | search | 8080 | `http://127.0.0.1:8888` |

Four of those nine need no action under Compose: `compose.yaml` sets the router's own three
to `http://router:8080...` itself, and `RAMEAU_OLLAMA_URL` is the one value you must supply.

**`RAMEAU_OLLAMA_URL` is required and has no default.** It is declared
`${RAMEAU_OLLAMA_URL:?...}`, so Compose refuses to *interpolate the file* without it -- which
means `up`, `build`, `config` and `ps` all stop with the registry's own message, not just
`up`. There is no default because every plausible default is wrong: inside a container,
loopback is the container. On Docker Desktop, and on Linux given the `extra_hosts` above,
`http://host.docker.internal:11434` reaches an ollama on the host.

The other five -- `embed`, `kb`, `arbiter`, `broadcaster`, `search` -- are optional, and a
blank value counts as unset rather than as a misconfiguration. Each one's `required_for` tag
in the registry records which capability it unlocks.

**Where to put the values.** `RAMEAU_OLLAMA_URL` and the five optional endpoints must reach
Compose's *interpolation* environment -- the shell, or a `.env` file in `deploy/`. Measured
with `docker compose config` on 2026-11-02:

```
cd deploy
printf 'RAMEAU_OLLAMA_URL=http://host.docker.internal:11434\n' > .env   # .gitignore covers it
```

Note that `deploy/profiles/compose.env`, which `deploy/README.md` step 1 tells you to create,
does **not** reach the router. Compose ranks the `environment:` attribute above `env_file:`,
and the router re-declares each endpoint as `${VAR:-}` in `environment:` -- so with nothing in
the interpolation environment, all five optional endpoints arrive at the router as the empty
string, overriding whatever that file says. The file does reach the `portal` service, which
does not read those variables. This is a defect in the stack rather than a subtlety to work
around; it is recorded here because a README that repeated the step without it would be
telling you to configure something that has no effect.

### Runtime config

```
mkdir -p config
cp <the four>.example.json config/   # then drop the .example from each name
```

The four are `fleet_config.json`, `capability_registry.json`, `capability_profiles.json` and
`bifrost_shell_policy.json`. Each is read by the application as
`Path(__file__).parent / "<name>.json"`, so `compose.yaml` mounts them one file at a time to
exact paths rather than mounting a directory -- a directory mounted onto `/app` would shadow
the application code. They are mounted read-only, so the container cannot drift from what you
edited, and `fleet_config.json` re-reads on mtime change on every request, so editing it
re-tiers a **running** router with no restart.

The `.example.json` sources do not exist yet. See step 4 above.

### The command sequence

```
cd deploy
docker compose up --build
```

and with the no-egress overlay:

```
cd deploy
docker compose -f compose.yaml -f compose.no-egress.yaml up --build
```

Neither example env file should be hand-edited. Regenerate both:

```
python shared/resolver.py --write-examples
```

That writes bytes rather than printing for a shell to redirect. The redirect forms
(`--env-example`, `--compose-example`) still exist, but on Windows Python's text-mode stdout
emits CRLF whatever shell runs it, so the content comes out right and the bytes wrong -- and
the test that compares file to generator then fails for a reason with no visible cause.

### How the no-egress profile differs

It is an overlay, applied on top of `compose.yaml`, and it changes exactly one thing: the
`rameau` network becomes `internal: true`. That removes the network's gateway, so containers
on it have no route off the host -- no internet, no LAN, no DNS beyond Docker's embedded
resolver -- enforced by the network stack rather than by anything the application promises.

It is an overlay rather than a Compose `profiles:` key because `profiles:` selects which
*services* run and cannot change a network's properties. Expressing it as a profile would
mean a second copy of every service on a second network, and two definitions of one service
drift.

Three limits, stated because they are otherwise assumed to be covered:

1. It constrains **egress from this network only**. A published port still accepts inbound
   traffic from the host, which is why those bindings are 127.0.0.1.
2. It does **not** isolate the services from each other. Everything on `rameau` can still
   reach everything else on `rameau`, on any port. Compose has no NetworkPolicy, so this is
   not the equivalent of the private deployment's NetworkPolicy objects.
3. `host.docker.internal` still **resolves**, and the route to the gateway is gone -- so a
   host-boundary endpoint times out rather than refusing, because there is no gateway to send
   a reset.

The consequence to plan for: because inference lives on the host, **generation will fail
under this overlay**. That is the useful result -- it demonstrates the router has no route
out. If you need generation to work as well, put a backend on the network yourself and point
`RAMEAU_OLLAMA_URL` at it by service name. What this overlay must not become is a quiet
re-opening of egress because generation failed.

### Accepting a build: do not stop at a health check

The stack defines **no healthcheck**, deliberately rather than by omission, and `/status`
returning 200 is not sufficient evidence that a build is good.

`bifrost_delegate.py` is read off disk with `ast` and is imported by nothing. A build context
computed from an import closure therefore drops it -- after which the tool loop refuses
**every delegated leaf**, fail-closed, while `/health` still answers 200. A healthcheck on
health would read green on exactly that failure and declare the container ready. So the
acceptance test has to **exercise one delegated leaf**; anything less tests that the process
started.

Note also that `/health` returns 200 whether the backends are up or down -- **the verdict is
in the body.** A caller that reads the status code (`curl -f`, a container healthcheck) reads
green while generation is failing. For liveness only, the application serves `/livez`.

### What works today, and what is blocked

Measured on this tree on 2026-11-02, Docker 29.4.2 / Compose v5.1.3.

| step | state |
|---|---|
| `python -m pytest shared/test_resolver.py` | **works.** 87 passed. |
| `python shared/resolver.py --write-examples` | **works.** Regenerates both example env files. |
| `docker compose -f deploy/compose.yaml config` | **works.** Parses and resolves, with `RAMEAU_OLLAMA_URL` set. |
| `docker compose -f deploy/compose.yaml -f deploy/compose.no-egress.yaml config` | **works.** Parses and resolves. |
| the same, with `RAMEAU_OLLAMA_URL` unset | **fails, correctly.** `required variable RAMEAU_OLLAMA_URL is missing a value`. This is the designed behaviour and CI asserts it. |
| `docker compose build` (either image) | **blocked.** |
| `docker compose up` | **blocked.** |
| copying the four runtime JSON files | **blocked.** They have not been derived. |

What a reader actually sees if they try `build` or `up` today:

```
unable to prepare context: path "...\RAMEAU-release\services\router" not found
```

The build context directories `services/router/` and `services/portal/` do not exist in this
repository. That is the first failure, and it arrives before anything is downloaded and
before any instruction in either Dockerfile is reached -- including the missing
`requirements.txt` that `deploy/README.md` and the router Dockerfile both name as the blocker.
Both are true; the missing context is simply the one you hit first. Nothing here is a
misconfiguration to debug: it is step 4 of the assembly order not having happened.

No image was built in producing this document, and no base layer was pulled.

## Provenance, stated plainly

RAMEAU is the public, feature-limited release of a routing layer that runs on a private
multi-host setup. The private deployment is not published and its history is not this
repository's history: this tree is assembled from an allowlisted, credential-scrubbed
subset, and its git history begins at that assembled state.

Anything this README claims about behaviour is either in the code in this repository or it
is removed from this README. Measured figures carry the configuration they were measured on,
or they are not quoted.

## Contributing

Not open for contributions until v0.1 is tagged. [SECURITY.md](SECURITY.md) describes how to
report a vulnerability, which is in scope now.
