# RAMEAU

A tier-routing layer for local language models. RAMEAU puts an OpenAI-compatible endpoint
in front of several locally hosted models of different sizes, classifies each incoming
request, and routes it to the smallest tier that can answer it -- falling through a cascade
when a tier is unavailable.

**Status: pre-release, and the source has landed.** Three build contexts now exist --
`services/router/` (34 tracked files, 33 of them in the image: its test directory is
dockerignored), `services/portal/` (28) and `services/shell-mcp/` (2) -- alongside the test
harness under `harness/`. See [RELEASE-SCOPE.md](RELEASE-SCOPE.md) for what v0.1 does and
does not contain, and why the repository was assembled in this order.

**All three images build.** This sentence replaces "neither image has yet been observed to
build", which was true when it was written and is the kind of claim this file retires by
citing a run rather than by deleting. Measured on the CI runner at `30ba18d`, every job
green:

| image | build context | image |
|---|---|---|
| `rameau/router` | 145.29 kB | 181 MB |
| `rameau/portal` | 862.58 kB | 70.1 MB |
| `rameau/shell-mcp` | 15.41 kB | 324 MB |

Those are the runner's own `transferring context` figures, not a local measurement. The
distinction is not pedantry: this tree is assembled on Windows, where a CRLF checkout of an
LF blob is larger than the blob, and the router context measures 145.80 kB locally against
145.29 kB on Linux. The portal's figure is identical on both because its bulk is woff2.

**Can you run it today? Closer, and the remaining gap is configuration rather than code.**
The stack builds and parses; what it cannot do is start without the four runtime JSON files,
whose schemas this repository does not contain -- see
[deploy/README.md](deploy/README.md#one-thing-is-still-not-ready). `shell-mcp` additionally
refuses to start without a bearer token, by design rather than by omission.

What IS measured today: **2,156 tests pass** against the harness (116 test files, 0 failures,
0 errors), the credential scanner reads clean over all 358 tracked files with a planted
address and a planted user-home path both caught first, all three images build, and the
Compose stack parses with three services.
[How to deploy the stack](#how-to-deploy-the-stack) documents the real path end to end and
marks which steps are verified.

Licensed under the [Apache License 2.0](LICENSE).

## Why this exists

Most routing layers assume a cloud provider and optimize for cost per token. RAMEAU assumes
the opposite: the models are already running on hardware you own, the marginal cost of a
token is electricity, and the thing worth optimizing is **latency against answer quality**.
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

**`shared/resolver.py` -- the one real module (654 lines, stdlib only).** Every endpoint in
its registry is resolved here, from the environment, with no fallback to a literal address.
Three properties, all of them behaviors rather than intentions:

This paragraph claimed "and nowhere else" until 2026-11-02, and that was too strong. The
session assembling the release reports that `fleet_config.json` -- one of the four runtime
files the stack bind-mounts, re-read on mtime change -- carries a `tiers[].base_url` per
tier, each a full `scheme://host:port`. Those are addresses the router talks to, and they
reach it without passing through the environment or through this registry. So the registry
covers what it covers; it is not the only address surface. That cannot be verified from this
repository, because `fleet_config.json` is not in it yet; RELEASE-SCOPE.md records it as an
assembly obligation rather than leaving the stronger claim standing.

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

**`.github/workflows/ci.yml` -- the CI gate.** Six jobs. The secret scan runs gitleaks over
every object in the full history -- not the working tree, which needs `--no-git` and is the
hook's separate advisory pass; on a clean CI checkout history is a superset, so nothing goes
unscanned, and this sentence said "working tree *and* full history" until 2026-11-03, which
was a claim about a check that had not run. Before trusting a clean report it plants a
generated high-entropy AWS-shaped key in a scratch repository outside the workspace and
requires gitleaks to flag it -- because a scan that cannot be shown to fire is not evidence.
The canary is generated rather than written literally for two reasons: a literal key would
itself be a finding in the workflow file, and AWS's documented example key is allowlisted by
gitleaks by default, which is how the first version of that control failed. The build job
byte-compiles, runs the tests, regenerates and diffs both env examples, asserts the
in-network profile contains no loopback, and validates both compose files by path -- with a
control that asserts the stack still **refuses** to resolve when `RAMEAU_OLLAMA_URL` is
unset. Its loopback check and its license check each run against a planted match and a planted
near-miss before they are pointed at the real file, through the same function rather than a
retyped copy of the pattern -- a control that retypes the pattern tests a different pattern. The image job builds both images **through compose**, because the thing most likely to
be wrong at step 5 is the wiring rather than either Dockerfile -- each service's context is
`../services/<name>` and its Dockerfile is outside that context -- and `docker build` would
exercise none of it. All three contexts now exist, so that job gates rather than skipping --
it did start gating the moment the directories appeared, with no edit, which is what it was
written to do. It asserts `compose.yaml` and the Dockerfiles are present *first*, so that
"skipped" can never be what a broken checkout looks like. Every build step is written to pass on an
empty tree and to start doing real work the moment source appears, so the gate never has to be
switched on later.

**The step-4 preflight** is the fourth job, and it exists because `RELEASE-SCOPE.md` had
accumulated a page of obligations on the moment the source lands -- each measured, each
correct, and each enforced by nothing but a reader remembering them. It asserts them in one
place: that both ignore files sit where Docker actually reads one, that no GPL `.jar` or model
weight is tracked, that `NOTICE` appears the moment any font does, and that every tracked file
carrying a shebang is `100755` *in the index* -- git's record is what travels, and this tree is
assembled on Windows, which has no executable bit. Seven further obligations need the
assembled source and are reported **deferred by name** rather than counted as satisfied, among
them the one that is silent in both directions: a router module outside `backends/` or
`skills/` is dropped twice, by the ignore file and by no `COPY` naming it, and fails at import
after a green build. The job reports one of four states by name, and says in its own output
which obligations it *cannot* check so that they fall to step 5 rather than passing quietly.

**No count in this repository is written beside the set it describes.** A sweep of the CI
gates reported "twelve matchers" when there were 21, and its own correction reported 21 when
there were 22 -- the list had been built by reading the jobs rather than by asking them, and
it had drifted twice. The `census` job now derives the population from the source: it
discovers the workflow files, the jobs, the files to scan, the declared matchers, the arms and
the step-4 obligations, asserts the correspondence **by name** -- an **unarmed check** and a
**stale arm** are different bugs and are reported separately -- and prints only numbers that
are the length of a list it built. A pattern added anywhere in a `run:` block without a
declaration fails that job with its file and line, which is what keeps the registry from being
a list in disguise, and each `Dockerfile.dockerignore` in the tree is discovered as a matcher
whose name comes from its path, so a third service's pattern set cannot arrive without an arm.
The floors it carries are floors and not pins: they catch a matcher deleted *together with*
its arm, which set equality cannot see, and they never go red on growth.

**`tools/ci/arms.sh` -- the failing arm for every gate, committed and run.** One command,
70 arms, under a minute, and the `arms` job runs it on every push: the census proves every
matcher *has* an arm, and only running them proves the arm still fires. Each clones this
repository, mutates the clone, runs one CI step against it and requires the check to catch the
mutation. A mutation that itself fails is reported as such rather than scored as a pass --
which is how a `git add` silently refused by `.gitignore` was caught reading green. The seven
step-4 obligations that need an assembled source tree are armed against a planted stub, so the
preflight reports them as *deferred with their arms already written* rather than merely
deferred.

**Every pattern-based matcher in CI has a planted positive and a planted near-miss**, and each
reports by name. Three times a check here has printed a clean verdict because its matcher could
not match its subject -- most recently the preflight's own flat-module `find`, where `*`
crossing a `/` in a `-path` glob made the most valuable assertion in the job inert on the day
it merged. An empty result is not an absence, and a matcher that flags *everything* prints
`control ok` exactly as loudly as one that flags nothing, so each of the **thirty-four** is
exercised both ways: the regexes, the `find` and `git ls-files` patterns, the `jq` selectors,
`grep`'s three-way exit, and the violation counter against sets of known size. The plants run on every
push whether or not a source tree exists, against trees built and discarded in the step.
Among them the `images` job builds a **planted build context** against each
`Dockerfile.dockerignore` and compares the transferred file list exactly -- which is how the
credential block in both files was found to be matched against a single path segment, so that
`backends/.env` and `public/leak.key` reached the context while a block headed "never, under
any circumstances" said nothing about either. Both now carry `**/`. See `RELEASE-SCOPE.md`.

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
acknowledgment rather than an aspirational one; a `.gitignore` written as the second of
three credential layers; `.gitattributes` normalizing the tree to LF because it is assembled
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
5. **Containerization.** The Compose stack and the offline profile. The files in `deploy/`
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
three license texts are in the release set under **SIL OFL 1.1**. That is compatible with
Apache-2.0 but is **not** Apache-2.0, so the third-party attribution has to be stated rather
than implied by the root `LICENSE`. [`NOTICE`](NOTICE) carries it; it landed with the fonts
in `245d1a9`, naming each face by family, and declaring that the faces are subset.

### Also outstanding

- **No dependency manifest.** `deploy/images/router/Dockerfile` needs
  `services/router/requirements.txt`, and the private repository's version is excluded from
  publication. The reasoning stands: inventing a pin set gives a reviewer no way to tell
  which pins were measured, and installing unpinned makes two builds of one commit different
  images, so a missing file that stops the build is the honest state of that gap.

  **The figure this paragraph used to quote -- "131 dependency decisions" -- is withdrawn.**
  It was not the router's. Reported to this session on 2026-11-02, measured on the private
  tree: 131 is the sum of two `bifrost-kb` requirements files, for a service that is not in
  this release at all, and the router's own manifest is five lines, each a public PyPI range
  specifier. This repository cannot check either number -- neither tree is in it -- so the
  figure is removed rather than replaced with another one that reads as measured here. What
  it means in practice is that the blocker is small: five packages already specified in a
  file reported to contain no credential, no index directive, no VCS reference and no local
  path.

  **Both open questions here are now closed, and the file is still deliberately absent.**
  John ruled on 2026-11-02 that `services/router/` is the release's router source, and the
  import closure was run on that tree and verified in both directions -- no import lacks a
  requirement, no requirement lacks an importer, and the closure recovers the manifest
  measured by hand, which is the control that it is not measuring something else. So the
  content is settled and no longer needs deriving.

  What is left is not derivation but **placement**, which belongs to step 4: committing the
  file alone would create a half-assembled `services/router/` with no `backends/`, no
  `skills/` and no `*.py`, which measurably turns CI red rather than green -- the `images`
  job stops skipping and `docker compose build` then fails on the rest. And one decision
  remains John's: the five lines are `>=` floors, and turning them into exact pins needs a
  reading from an environment that demonstrably serves traffic, which exists only on the
  private side. RELEASE-SCOPE.md carries the closure as evidence so step 4 is a
  transcription rather than a re-derivation.
- **The four runtime JSON files** (`fleet_config.json`, `capability_registry.json`,
  `capability_profiles.json`, `bifrost_shell_policy.json`) are dispositioned as `.example`
  derivations, and **their schemas have not been derived.** `deploy/config/*.example.json`
  exist, but each one is a placeholder that records what this repository establishes about the
  file and what it does not, in `_`-prefixed prose keys; no field of any of the four schemas is
  reproduced, because this repository contains no sample, loader, test or schema for any of
  them. Of the four, only `fleet_config.json` has an established *purpose* — it carries the
  tier mapping and re-reads on mtime change. The other three are named, mounted, and otherwise
  undescribed, and `bifrost_shell_policy.json` additionally sits on one of the four open scope
  questions below. Copying the examples into `deploy/config/` will not start the router; what
  it does is give `compose.yaml`'s bind mounts real files, which matters because Docker creates
  an empty *directory* at a missing bind source.
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

Three services on one bridge network named `rameau`.

| service | what it is | container port | published by default |
|---|---|---|---|
| `router` | the OpenAI-compatible endpoint, the pre-generation classifier, tier selection and the cascade, plus the routing cache -- **off unless switched on**, see below | 8080 | `127.0.0.1:18080` |
| `portal` | the existing web UI, as it stands -- nginx serving a Vite bundle | 80 | `127.0.0.1:13110` |
| `shell-mcp` | the delegated-shell MCP server, static-analysis subset only: `run_ruff`, `run_mypy`, `run_checks`. Bearer-authenticated, and it **refuses to start** without a token of at least 32 characters | 8086 | `127.0.0.1:8086` |

Override the published addresses with `RAMEAU_ROUTER_PUBLISH`, `RAMEAU_PORTAL_PUBLISH` and
`RAMEAU_SHELL_MCP_PUBLISH`.
Both defaults bind **127.0.0.1 explicitly**: a bare `18080:8080` would bind `0.0.0.0` and
publish a router that proxies to a local model to every machine on the host's network. If
you want a port exposed, override the variable, so that the exposure is something somebody
wrote down.

Inside the network the router is `http://router:8080`. That is the port the container
listens on and it is **not** the port the private deployment published -- an in-container URL
built from a host-side port resolves to nothing.

### The routing cache ships, and ships OFF

The router image contains the routing cache: `routing_cache.py` and
`routing_cache_serve_path.py`, byte-identical to the published source under `harness/router/`
with a test pinning them that way. It answers a request from a previous classification when a
prompt's embedding is a near neighbour, skipping the classifier.

**It is inert until an operator sets `BIFROST_ROUTING_CACHE_SERVE`.** With the variable unset,
`serve_enabled()` is false, no cache object is constructed, no persisted file is read, and the
classifier runs exactly as it would without any of this. That is the shipped default and it is
not a soft default: the switch is read before any other work the feature would do.

Turning it on needs an embedding endpoint, `RAMEAU_EMBED_URL`. There is **no literal
fallback** -- the published source defaults this to a private LAN address, and this release
resolves it through `shared/resolver.py`, which raises instead. An unconfigured embedder
therefore degrades to plain classification and logs why, rather than reaching for somebody
else's network.

| variable | effect | default |
|---|---|---|
| `BIFROST_ROUTING_CACHE_SERVE` | the switch; nothing is built without it | unset — off |
| `RAMEAU_EMBED_URL` | where embeddings come from; required once serving | unset — raises |
| `BIFROST_ROUTING_CACHE_SERVE_BANDS` | which bands it may answer | all four |
| `BIFROST_ROUTING_CACHE_THRESHOLD` | cosine similarity floor for a hit | 0.85 |
| `BIFROST_ROUTING_CACHE_CONF_FLOOR` | confidence a stored entry needs to be usable | 0.7 |

`/status` reports the live state under `routing_cache`, separately from whether the switch is
set, because those come apart: the switch is read at startup, so exporting it into a running
container changes the reported flag and nothing else.

**The autopilot is not included.** It was considered alongside the cache and deliberately left
out; `RELEASE-SCOPE.md` records that as a ruling rather than an oversight.

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
services -- but not in the same way, and the portal's is worse than this README said until
2026-11-02.

- **The router starts and every generation request fails.** The address is resolved per
  request, so the process comes up and the failure arrives later, looking like a model fault.
- **The portal does not start at all.** nginx resolves a *literal* upstream name at
  config-load time, so an unresolvable one is fatal: `[emerg] host not found in upstream`,
  and the container exits and crash-loops rather than serving anything. This README
  previously said the SPA kept serving while the panels sat empty. That is request-time
  behavior, and it is wrong for a literal upstream -- measured by running the pinned
  `nginx:alpine` against a literal name that does not resolve.

The portal's `nginx.conf` is **not in this repository** -- it arrives with the unassembled
`services/portal/` build context. What the correction rests on is therefore worth stating
precisely, because the two parts are not equally strong. The nginx behavior was
**reproduced here**, by running the pinned `nginx:alpine` against a literal upstream name
that does not resolve and watching the container exit; that is a measurement, not a report.
What the assembling session supplied is only *which* routes are literal: of eight live
`proxy_pass` directives, five are literal `host.docker.internal` upstreams resolved at
config load, and three are variable upstreams behind a single `resolver` directive, resolved
per request. No other literal host exists in the file. Three further directives are
commented out, which is why a bare `grep -c proxy_pass` returns eleven.

The verdict does not actually depend on that split: one unresolvable literal is enough to
fail config load. And the file's variant is settled -- the release derives from the live
one. See RELEASE-SCOPE.md.

### What you need

- Docker with Compose **v2.24 or newer** -- `compose.yaml` uses `env_file: required: false`.
- An **ollama you already run**, reachable from this host.
- The four runtime JSON files in `deploy/config/` (see below). The `.example.json` files exist;
  their **schemas** do not, so copying them is not yet enough to run.

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

**Where to put the values, and it is two places.** The split is not elegant and it is not
avoidable, so it is stated rather than smoothed over. Measured with `docker compose config`
on 2026-11-02, Compose v5.6.1.

**The five optional endpoints go in `deploy/profiles/compose.env`** -- `RAMEAU_EMBED_URL`,
`RAMEAU_ARBITER_URL`, `RAMEAU_KB_URL`, `RAMEAU_BROADCASTER_URL`, `RAMEAU_SEARCH_URL`. That is
what `deploy/README.md` step 1 has always told you to do, and as of this change it is also
what works. For these five the shell and `deploy/.env` do **not** reach the router.

**`RAMEAU_OLLAMA_URL` goes in the shell or in `deploy/.env`** -- and *not* in
`profiles/compose.env`, which is read too late for it:

```
cd deploy
printf 'RAMEAU_OLLAMA_URL=http://host.docker.internal:11434\n' > .env   # .gitignore covers it
```

It is declared `${RAMEAU_OLLAMA_URL:?...}`, and `:?` is resolved at *interpolation* time,
before any `env_file` is read. Putting it in `profiles/compose.env` leaves Compose still
refusing, with the registry's own message -- which now names the right file.

**Why the five moved.** Until this change they were re-declared under the router's
`environment:` key as `${VAR:-}`, and `environment:` outranks `env_file:`. With nothing in
the interpolation environment each one resolved to the **empty string** and overrode whatever
the operator had written, and because the resolver treats empty as unset the router then
reported them as not configured. Nothing failed: the stack came up, `config` parsed, and the
endpoints were simply absent. Three forms were measured, and only one lets the file through:

| form under `environment:` | shell unset | shell set |
|---|---|---|
| `VAR: ${VAR:-}` | `""` -- **overrides the file** | shell wins |
| `- VAR` (list form) | `null` -- **also overrides it** | shell wins |
| not declared at all | the file's value reaches the router | the file still wins |

There is no form that takes the file as a *fallback* under a shell override; one source has
to win, and for these five it is the operator's own file. The router's own three URLs stay
hardcoded in `compose.yaml` and deliberately keep overriding the file, because the file
addresses the router by service name on a port a host-side example may have carried over.
CI asserts all of this directly -- it writes a sentinel into `profiles/compose.env` and
requires it to survive into the router's resolved environment.

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

The `.example.json` sources exist, and **each is a placeholder rather than a derived schema.**
They carry `_`-prefixed prose keys naming what this repository establishes about the file and
what it does not; no field of any of the four is reproduced, because nothing here shows one.
Copying them gives the bind mounts real files — which is worth having, since Docker creates an
empty *directory* at a missing bind source and the resulting `IsADirectoryError` surfaces far
from its cause — but it will not start the router. The schemas arrive with step 4 above.

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

**That criterion is SUSPENDED as of 2026-11-02, and the paragraph above is about an artefact
this release does not ship.** A measurement of the router source ruled into scope finds no
`bifrost_delegate.py`, no tool loop and no function-calling surface, against a positive
control in the same sweep. So there is no delegated leaf to exercise — and the insufficiency
runs the other way: four of the five requirements resolve at module scope and the fifth
cannot be absent from a serving process, so for the ruled scope a 200 *does* discharge the
manifest question. RELEASE-SCOPE.md carries the measurement, a staged replacement that tests
what a 200 genuinely does not prove, and the scope ruling this waits on.

Note also that `/health` returns 200 whether the backends are up or down -- **the verdict is
in the body.** A caller that reads the status code (`curl -f`, a container healthcheck) reads
green while generation is failing. For liveness only, the application serves `/livez`.

### What works today, and what is blocked

Rows marked 2026-11-02 were measured on Docker 29.4.2 / Compose v5.1.3; the build row is from
the CI runner at `30ba18d` on `main`, docker 28.0.4.

| step | state |
|---|---|
| `python -m pytest` (the whole suite) | **works.** 2,156 passed, 5 skipped, 1 xfailed, 0 failures, 0 errors. |
| `python -m pytest shared/test_resolver.py` | **works.** 87 passed when last measured alone. |
| `python shared/resolver.py --write-examples` | **works.** Regenerates both example env files, and CI regenerates-and-diffs to keep that true. |
| `docker compose -f deploy/compose.yaml config` | **works.** Parses and resolves all three services, with `RAMEAU_OLLAMA_URL` set. |
| `docker compose -f deploy/compose.yaml -f deploy/compose.no-egress.yaml config` | **works.** Parses and resolves. |
| the same, with `RAMEAU_OLLAMA_URL` unset | **fails, correctly.** `required variable RAMEAU_OLLAMA_URL is missing a value`. This is the designed behavior and CI asserts it. |
| `docker compose build` | **works.** All three images build on the runner. |
| `docker compose up` | **blocked, on configuration rather than code.** The router needs the four runtime JSON files and this repository has not derived their schemas; `shell-mcp` needs a bearer token you generate. |
| copying the four runtime JSON files | **works, and does not get you a running router.** `deploy/config/*.example.json` exist as placeholders; their schemas have not been derived. |

Until step 4 landed, `build` and `up` both failed before reaching any instruction in any
Dockerfile -- `unable to prepare context: path "...\services\router" not found`, which arrived
before anything was downloaded. That is no longer reachable, and it is worth knowing it was
never a misconfiguration to debug: it was step 4 of the assembly order not having happened.
What remains between a green build and a running stack is the runtime config, which is a
smaller and much better-defined gap.

All three images now build from this tree, on the CI runner at `30ba18d`. Before step 4 the
Dockerfiles had only been exercised against a throwaway stub context, created and then
discarded; the digest-pinned base images resolved when that was done on 2026-11-02. Nothing
from that stub is in this repository -- `services/` holds the assembled source and nothing
else, which was the point of keeping it absent rather than ignored until the day it appeared.

## Provenance, stated plainly

RAMEAU is the public, feature-limited release of a routing layer that runs on a private
multi-host setup. The private deployment is not published and its history is not this
repository's history: this tree is assembled from an allowlisted, credential-scrubbed
subset, and its git history begins at that assembled state.

Anything this README claims about behavior is either in the code in this repository or it
is removed from this README. Measured figures carry the configuration they were measured on,
or they are not quoted.

## Contributing

Not open for contributions until v0.1 is tagged. [SECURITY.md](SECURITY.md) describes how to
report a vulnerability, which is in scope now.
