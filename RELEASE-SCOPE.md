# Release scope for v0.1

This document exists so that the README never has to claim something the code does not do.
It is the contract: anything cut is listed here as cut, by name.

## Assembly order, and why it is this order

This repository was created as scaffolding first and will receive source second. That is
deliberate, not an accident of scheduling.

1. **Scaffolding** — licence, ignore rules, secret-scanning CI, this document. *Done.*
2. **Allowlist** — an explicit, mechanically generated list of which paths from the private
   archive enter this tree. A release built by excluding what looks internal will miss
   something; a release built by including only what is approved will not.
3. **Scrub** — credential removal, verified by more than one independent scanner over both
   the working tree and the full history of the source archive.
4. **Source lands** — assembled from the allowlist, into a history that begins here.
5. **Containerisation** — the Compose stack and an offline profile.
6. **Hardening and release** — documentation, an independent final scan, then the tag.

Step 4 does not begin until step 3 reads clean. The ordering is the whole point: a
credential committed to a private repository has still left the machine it was on, and no
later rewrite un-sends it.

## In scope for v0.1

| area | included |
|---|---|
| OpenAI-compatible endpoint | chat completions, streaming and non-streaming |
| request classification | the pre-generation classifier and its tier bands |
| tier selection and cascade | fallthrough when a tier is unavailable |
| request cache | the near-duplicate cache in front of the classifier |
| configuration | a single documented config surface plus `.env.example` |
| deployment | Docker Compose, including a profile that runs with no egress |
| observability | structured logs and basic request metrics |
| the portal | the existing web UI, as it stands -- **ruled in by John 2026-10-27** |
| autopilot | the planning graph and its orchestration -- **ruled in by John 2026-10-27** |

The last two rows were added by ruling, not by drafting, and the reason is recorded
below under "A scope gap and a scope contradiction, both settled by ruling" -- the
census had already selected both families and this table did not name them.

## Explicitly NOT in v0.1

Listed, not forgotten. Each is either host-specific, depends on infrastructure that is not
being published, or is large enough to deserve its own release.

| cut | reason |
|---|---|
| overnight orchestration / scheduled self-improvement | operationally entangled with a specific fleet. **Narrowed 2026-10-27:** this cuts the scheduled overnight RUNNER and its fleet wiring, NOT the autopilot planning graph, which is ruled in above |
| the agent sandbox | depends on a Linux-only isolation mechanism not present on every host |
| NPU classification path | hardware-specific, and the measured failure rate on the one host that has it is too high to publish |
| quantum-related experiments | research, not product |
| workflow-automation integration | third-party service wiring, host-specific |
| messaging bridges and notification transports | credential-bearing by nature |
| the portal **redesign** | its own track, with its own design review. **Clarified 2026-10-27:** this only ever cut the redesign; the existing portal is ruled in above |
| GitOps / continuous-deploy layer | a ship-path concern, not a routing concern |
| multi-host fleet management | the private deployment's shape is not the product |
| benchmark harness and published throughput figures | figures belong to hardware; publishing them would invite machine-to-machine comparison, which this project explicitly does not do |

If a week of the release schedule slips, the slack is taken from the bottom of the in-scope
table — never from the scrub or the scanning steps.

## Known assembly obligations, recorded before they can be forgotten

From the allowlist census of 2026-10-27, after the operator rulings of that date:
**282 of 7,009 source paths selected, 3,757,558 bytes.** Transforms: 200 straight
copies, 68 needing the resolver sweep, 7 derived as `.example`, 7 needing documentation
scrubbing. Twenty paths remain undecided and are triaged separately.

Earlier figures circulated for this census — 286/3.4 MB, and later 294/4.0 MB with 207
sweep files — are both superseded. The first predates a re-run; the second predates the
rulings that removed twelve paths and corrected 130 transforms.

### A scope gap and a scope contradiction, both settled by ruling (2026-10-27)

**37% of the selected set was scope this document did not grant.** Measured against
`allowlist.json`: 53 unique RELEASE paths, 1,392,077 of 3,757,558 bytes, in families the
tables above either cut or never mentioned.

| family | RELEASE paths | bytes | what the tables said |
|---|---|---|---|
| portal | 35 | 872,218 | **nothing** — the in-scope table named no UI at all |
| autopilot | 6 | 332,580 | cut, as "overnight orchestration / scheduled self-improvement" |
| `igor_tool_loop.py` | 1 | 110,359 | nothing |
| `bifrost_delegate.py` | 1 | 37,616 | nothing |
| decomposer | 4 | 29,503 | nothing |
| shell-mcp | 6 | 9,801 | cut, as "the agent sandbox" |

These were two different defects and it matters which was which. For **autopilot** the cut
list named the family and the census selected it anyway: a contradiction, where two parts
of one document disagreed and a reader could cite either. For the **portal** there was
nothing to contradict — the in-scope table lists seven areas and none of them is a user
interface, so 872 KB of UI was being assembled under a scope that never addressed it. A
gap is the worse of the two, because a contradiction announces itself and a gap does not.

**John ruled on 2026-10-27: portal and autopilot stay in.** Both now have in-scope rows,
the two cut rows are narrowed to say what they actually cut, and the compose service list
can be written against this table.

**The ruling covers two of the six families, and the other four remain open.**
`igor_tool_loop.py`, `bifrost_delegate.py` and the decomposer are selected and unnamed by
either table — the same gap as the portal, not yet closed. `docker/shell-mcp/` is the
sharper case: the cut list names "the agent sandbox" directly and six shell-mcp paths are
selected, so that contradiction is still live. Nothing here extends the ruling to them;
they are listed so the next reader sees four open questions rather than a settled table.

- **A `NOTICE` file is required at the root.** Nine `.woff2` faces (Instrument Sans,
  Instrument Serif, JetBrains Mono) and their three OFL licence texts are in the release
  set. SIL OFL 1.1 is compatible with Apache-2.0 but is *not* Apache-2.0, so the third-party
  attribution has to be stated rather than implied. This file does not exist yet because the
  fonts have not landed; it goes in with them, not before.
- **`docker/.env.example` must be read line by line before it is copied.** It was classified
  into the release set on its *filename* alone. A `.example` suffix is a convention, not a
  guarantee, and this is exactly the kind of file where the convention is most often broken.
- **`shared/resolver.py` — DISCHARGED** in `3a59271`, with its defects fixed in the
  commit that carries this note. It exists, it is covered by tests, and it resolves every
  endpoint from the environment with no fallback to a literal address.

  What it was actually needed for turned out not to be what this note assumed. The
  archive's addresses had already been redacted *in place*, which left fourteen `getenv`
  call sites across eight files defaulting to values like `http://192.168.x.4:11436` —
  not a leak, and not a working value either. The resolver's job is to refuse those
  loudly rather than fail as a DNS lookup several layers below the real mistake.

- **The two example env files are generated, and are deliberately two files.**
  `.env.example` carries loopback values for running on a host;
  `deploy/profiles/compose.env.example` addresses services by name for running inside a
  container network. One file could not serve both: inside a container, loopback is the
  container itself, so a host-oriented file silently points every endpoint at nothing.
  CI regenerates both and diffs them, so "generated from the registry" is a gate rather
  than a claim.

- **200 of the 282 selected paths are a safe byte copy.** 68 need the resolver sweep, 7
  must be derived as `.example` files, and 7 need documentation scrubbing. The assembly
  is still a transform rather than a copy, but it is a third of the size this note first
  recorded: the sweep had been assigned by file *extension*, and **130 of the 207 files
  it named were found to carry no private literal and were reclassified as straight
  copies**, with a further 9 leaving the release set entirely. It is now decided by
  reading the file.

  An earlier draft of this line said 145. That figure came from an exploratory scan
  using five host-and-path patterns, while the census that decides the transform uses
  seven — it also counts a redacted octet and a private IP. The eight-file gap is
  exactly that class: files such as `fetch_guard.py` and `vite.config.js` carry
  `192.168.x.N` placeholders and none of the host patterns, and the census is right to
  keep them in the sweep, because a redacted placeholder is a non-working value. 130 is
  the number this sentence describes and the only one a reader can reconstruct from
  `allowlist.json`; 145 answered a different question.
- **Every file in the planned `docs/` slot is new writing.** The source archive's entire
  `docs/` tree is excluded as internal, so no documentation carries over.
- **`bfg-1.14.0.jar` (GPLv3, 13.8 MB, two copies) is excluded** and must be named in the
  wipe ledger. A GPL artefact reaching an Apache-2.0 release would be a licence conflict,
  not merely unwanted weight.

No model weights exist anywhere in the source archive, so none can leak into the release.

### Containerisation findings of 2026-11-02, recorded before step 5 begins

Both Dockerfiles were exercised against a throwaway stub build context, created outside git
and discarded; no stub reached this repository and `services/` is still absent here rather
than ignored. Four defects were found and discharged. Two obligations remain, and they
remain because this repository cannot settle them, which is why they are written here
rather than left in commit messages.

- **The pre-push credential gate was not running — DISCHARGED in `28f7c82`.** It was stored
  mode `100644`, so git skipped it and pushed anyway, reporting that as a `hint:` beside a
  *successful* push. `README.md` and the hook's own header describe it as fail-closed and
  as the only layer that blocks a credential before the object reaches the remote. It had
  never run once.

  **This generalises into an obligation on step 4.** This tree is assembled on Windows,
  which does not carry the executable bit, and `.gitattributes` normalises line endings
  only — `* text=auto eol=lf` has nothing to say about file modes. So every file arriving
  at step 4 whose job depends on being executable will arrive `100644` and will fail
  *silently*. Assert the mode after assembly, and do not infer it from the file working
  when invoked by hand: `bash <file>` works regardless.

- **Both `.dockerignore` files were read by nothing — DISCHARGED in `97727b8`.** They sat
  in `deploy/images/<svc>/` while the build contexts are `services/<svc>/`, and Docker
  reads an ignore-file only from the context root or as `<dockerfile-name>.dockerignore`
  beside the Dockerfile. Every pattern in both was inert, which left `COPY *.py ./` bounded
  by nothing and the router list's "never, under any circumstances" block — `.env`,
  `*.pem`, `*.key`, `.netrc`, `.git-credentials` — not in force at all.

- **`profiles/compose.env` did not reach the router — DISCHARGED in `c4e3620`.** The five
  optional endpoints were re-declared under `environment:`, which outranks `env_file:`, so
  each resolved to the empty string and overrode the operator's file; the resolver treats
  empty as unset, so they were reported as not configured. CI now writes a sentinel into
  that file and requires it to survive into the router's resolved environment.

- **`npm ci` can exit 0 having installed nothing — GUARDED in `fdf4008`.** A fatal failure
  in the pinned node image printed `npm error Exit handler never called!` and returned
  success, after which the build ran three more instructions and died at
  `sh: vite: not found`.

#### SETTLED: the release derives from the LIVE `nginx.conf`, and the symptom table is exact

`nginx.conf` is **not in this repository** — a sweep for `nginx*.conf` and
`*.conf.template` finds nothing, and it arrives with the unassembled `services/portal/`
context. Four statements here were therefore predicting the behaviour of a file this
repository does not contain, and four of them were wrong about the symptom. The shapes
below were reported by the session assembling the release, with no values; the nginx
behaviour they are read against was reproduced here against the pinned `nginx:alpine`.

Eight live `proxy_pass` directives. Three more are commented out, which is why a bare
`grep -c proxy_pass` returns eleven and misleads.

| form | count | resolved |
|---|---|---|
| `proxy_pass http://host.docker.internal:<port>;` — literal | 5 | at config load |
| `set $up_X http://${ENVVAR}:<port>;` then `proxy_pass $up_X;` | 3 | at request time, via the single `resolver` |

Swept for any other literal host: there is none. So only the three deferred routes can 502
while the SPA keeps serving, and an unresolvable literal is fatal when nginx parses its
config. **The verdict does not depend on the 5/3 split** — one unresolvable literal is
enough to fail config load, so "the portal does not start at all" holds even if someone
disputes the counts. Note that `deploy/compose.yaml`'s own comment independently names five
`host.docker.internal` routes, which agrees.

**Which variant: the live one.** The private tree holds two and they are not byte-identical.
The live file carries one `resolver` directive and three `set $` directives; the
`.CORRECTED` candidate, which sits in a `_defect-fixes/` directory, carries neither and
**was never applied**. So the release derives from the live variant and this table does not
flip. A divergent unapplied variant exists and would change it if adopted — adopting it is a
decision separate from the release, and is not taken here.

**Scope of the failure mode, so no one reads it as the shipped stack being broken.**
`deploy/compose.yaml` passes `extra_hosts: host.docker.internal:host-gateway`, so inside
this stack the name resolves and none of this bites. It applies to the portal image run
*outside* the stack without the equivalent `--add-host`.

#### SETTLED: `fleet_config.json` is a second address surface BY INTENT — fold nothing

Answered 2026-11-02 by the assembling session, as shapes with no values: the file carries
endpoint addresses. Each entry of a top-level `tiers` list has a `base_url`, a full
`scheme://host:port` with an explicit port; nine in the private router's copy, ten in an
older backup. The file has three top-level keys.

**This is deliberate, and the registry structurally cannot do this file's job.** The router
`stat`s the file and reloads it when its mtime changes. Environment variables are fixed at
process start, so an environment-resolved registry cannot re-tier a running router — and
re-tiering without a restart is the entire point of the mtime re-read. The boundary:

- `shared/resolver.py`'s registry owns **endpoints fixed at deploy time**.
- `fleet_config.json` owns **backends re-tiered at run time**.

Two surfaces, two lifetimes, both intended. So the registry's guarantee is **bounded, not
defective**, and no text in this repository should imply it ought to have absorbed the other
— the earlier "and nowhere else" wording did imply that, and was corrected rather than
softened. Nothing is to be folded.

**One detail that bounds what a test may assert.** The same key has carried two host forms:
named hosts in the router's copy, dotted quads in an older backup. An assertion that the
file holds only service names would be true of one copy and false of the other. The shape
that holds for both is `^https?://[^/]+:\d+$`.

#### OPEN: the generated compose example addresses six services the stack does not define

Verified in this repository, not reported: `shared/resolver.py`'s `COMPOSE_SERVICE` maps its
nine endpoints onto seven service names, and `deploy/compose.yaml` defines two. Six are
addressed and undefined — `arbiter`, `broadcaster`, `embed`, `kb`, `ollama`, `search`.

```
resolver COMPOSE_SERVICE targets: arbiter broadcaster embed kb ollama router search
compose.yaml defines:             portal router
addressed but NOT defined:        arbiter broadcaster embed kb ollama search  (6 of 7)
```

`compose.yaml` explains at length why those services are absent — none of their images is
part of v0.1 — and the generated example is correct for a deployment that *does* run them.
But an operator who copies it unedited into this stack gets six names the container network
cannot resolve. **This belongs to whatever generates
`deploy/profiles/compose.env.example` from the registry, not to the deploy documentation**,
which is why it is recorded here rather than papered over with a note. Still open.

#### Three decisions left deliberately untaken, recorded so nobody takes them quietly

- **The `BIFROST_HOST` rename.** It changes the deploy surface of a public repository.
- **The `HEARTH_VEGA8_URL` alias** in `shared/resolver.py`. Not the same class as the above:
  an alias *accepts* an old name, whereas a compose variable *asks an operator to set* one.
  The registry already keeps it only as a compatibility alias and says why.
- **The pre-rewrite SHA residue.**

#### RULED 2026-11-02 BY JOHN: `services/router/` is the release's router source

The two candidates and their measured differences are kept below, because the ruling is only
legible against them, and because a later reader should be able to see that it was a decision
rather than an inference.

| | `services/router/` | `bifrost-router/` |
|---|---|---|
| `.py` files | 12 | 11 |
| subdirectories | `backends/` | `backends/`, `skills/` |
| carries `requirements.txt` | yes | no |
| distinct third-party imports | 5 | 9 |

**This settles the manifest's contents. It does not settle the assembly.** `skills/` exists
only under the other tree, and `COPY skills/ /app/skills/` needs it present or the build
fails on it. So step 4 draws from both trees, and assembling `services/router/` is still not
a copy of one directory. That obligation stands unchanged below.

#### DISCHARGED: the import closure, run on the ruled tree and verified in both directions

Run on the private side on 2026-11-02, because the closure needs the tree this repository
cannot see. Nothing here can check it; it is recorded so that step 4 is a transcription
rather than a re-derivation.

The decisive result is that **the shipping set turns out to be the whole tree**, so the
closure is a remap that happens to coincide with a copy — which is worth stating precisely,
because it means no third-party import is reachable only from excluded code:

```
ignore file   ** deny-all, then re-admit requirements.txt, *.py, backends/, backends/**,
              skills/, skills/** -- then re-deny .env*, keys, tests/, test_*.py,
              *_test.py, __pycache__, *.pyc and the rest
COPY groups   requirements.txt | backends/ | skills/ | *.py  (TOP LEVEL ONLY)

shipping set  8 top-level .py + 4 under backends/ = 12 files. No .py exists in that
              tree outside those two groups, and none of the 12 matches test_*.py
              or *_test.py.

closure       28 top-level imports -> 16 stdlib, 7 local, 5 third-party
              uncovered imports: 0        unused requirements: 0
```

**Positive control holds:** the closure recovers all five lines of the manifest measured by
hand. A closure that cannot reproduce that is measuring something else, which is the only
reason to trust the zeros above.

So the manifest is complete and minimal **for the file set the release actually ships**, not
merely for the tree it came from. That distinction was the whole point of requiring a remap.

The five package names are still not transcribed here. The earlier reason — that a scope
document invites someone to copy them and skip the verification — has expired now that the
verification is done, but the better one remains: release source does not belong in a scope
document, and the first place those lines should appear is a `requirements.txt` that survived
this closure. They are parked durably on the private side with this measurement beside them.

#### DECISION NOT TAKEN HERE: the file is not placed yet, and the reason is measured

Placing `services/router/requirements.txt` now would leave that directory holding a manifest
and none of the source it describes — no `backends/`, no `skills/`, no `*.py`. That is not
merely untidy. It measurably **turns CI red**:

```
neither context present (today)      -> present=false, images job skips, green
a lone requirements.txt in services/router/
                                     -> present=true, "HALF-ASSEMBLED TREE",
                                        docker compose build runs and fails
```

The `images` job's half-assembled-tree branch exists for exactly this and fires correctly, so
the half-step is caught rather than silently shipped. Selecting a file for the release is an
assembly decision in any case; this one waits for step 4, with the closure above standing in
for the work so nothing is lost by waiting.

#### STILL JOHN'S: floors or exact pins

The five lines are `>=` floors. Floors are honest about what was tested and leave the image
unreproducible; pins are reproducible but assert that a specific version works, which is only
true if measured. A pin would have to be read from an environment that demonstrably serves
traffic — a `pip freeze` filtered to those distributions and their transitive closure, with
each pin's source recorded. No such environment is reachable from the release side, so absent
one, **keep the floors** rather than invent pins.

#### TWO NAME TRAPS FOR WHOEVER WRITES THE FILE

- The metrics client's **import name uses an underscore and its distribution a hyphen**. pip
  normalises the two, so a line written either way installs, but the mapping is to be read
  from `importlib.metadata.packages_distributions()` in an environment where the packages are
  installed — never from recall. This is the same class as `yaml` → `PyYAML`.
- **`uvicorn[standard]` is not `uvicorn`.** Dropping the extras marker silently removes the
  parts that server needs in order to serve, and the Dockerfile's `CMD` already depends on
  them. Named here because `uvicorn` is already public in this repository; the other three
  packages are not named for the reason above.

#### HOW IT MUST BE VERIFIED, because a green build proves almost nothing here

A successful `docker build` proves the `COPY` groups resolve. A missing requirement fails at
**import time inside the container, after a green build**. So:

- start the container and **exercise one delegated leaf**. `/status` returning 200 is
  explicitly insufficient, for the `bifrost_delegate.py` reason recorded above — it is read
  off disk with `ast`, imported by nothing, and its absence makes the tool loop refuse every
  delegated leaf while `/status` still answers 200.
- keep `--no-cache-dir`, as the Dockerfile already does, so a stale wheel cache cannot make a
  broken manifest look installable.

#### OBLIGATION: `skills/` and the manifest live in DIFFERENT private trees

`COPY skills/ /app/skills/` needs that directory present in the context or the build fails
on it, exactly as it currently fails on `requirements.txt`. Reported: `skills/` exists only
under `bifrost-router/`, holds nine files, all Markdown, zero `.py` — so it contributes no
third-party import and cannot change the manifest.

The consequence is the point: **the directory the Dockerfile needs and the manifest it needs
were measured in different trees**, so assembling `services/router/` is not a copy of one
directory. Whoever runs step 4 has to know that before starting.

**Do not create an empty `skills/` to get past the `COPY`.** It would make the build go green
while shipping a router with no skills — the same failure shape as every other item in this
section.

#### A withdrawn figure, recorded because it was load-bearing

Three places in this repository — `README.md`, `deploy/README.md` and
`deploy/images/router/Dockerfile` — justified the missing manifest with "131 dependency
decisions made by guesswork". That figure was another service's: reported as the sum of two
`bifrost-kb` requirements files, for a service not in this release. The router's own manifest
is five lines.

It is withdrawn in all three rather than replaced, because this repository can check neither
number. The reasoning it carried is sound and never depended on the size, which is why the
reasoning stays. But the figure was the stated reason the build is *allowed* to stop, and a
measurement read off another instrument's output is precisely what `PROVENANCE` in
`shared/resolver.py` exists to prevent — the `search` endpoint's port was wrong the same way.

#### The compose wiring has now been exercised once, against a stub

Recorded because until 2026-11-02 it had never run at all, and because `deploy/compose.yaml`
names it as the part most likely to be wrong at step 5: each service's build context is
`../services/<name>` while its `dockerfile` is `../../deploy/images/<name>/Dockerfile` — a
Dockerfile outside its own context, resolved relative to the context. `docker build`
exercises none of that, and the `images` CI job that would is skipped until step 4. So the
one path nobody had run was the one identified as likeliest to break.

Run through `docker compose -f deploy/compose.yaml build` against a throwaway stub context
created outside git and discarded. Three results, all of them what the stack intends:

- **The wiring resolves.** Both build definitions loaded and both contexts transferred in one
  invocation — 446 B for the router and 61 kB for the portal, which also shows the ignore
  files bounding the contexts rather than uploading the trees.
- **`RAMEAU_COMMIT` reaches the image through compose's own `${RAMEAU_COMMIT:-unknown}`
  declaration**, not via `--build-arg`, which would have bypassed the declaration and tested
  nothing. Set, it arrives as both the `ENV` and the
  `org.opencontainers.image.revision` label; unset, the label reads `unknown`, so the default
  in the declaration is what applies.
- **The required-variable control holds for `build`, not only for `config`.** With
  `RAMEAU_OLLAMA_URL` absent, `docker compose build` refuses during interpolation with the
  registry's own message. This had only ever been asserted for `config`; `build` is now
  known to be covered by the same refusal.

What this does NOT establish: anything about the assembled source. A stub proves the wiring
and the instruction order; the manifest is still decided by the acceptance test in
deploy/README.md, because a missing requirement fails at import time after a green build.

One environment caveat, so the result is not read as stronger than it is: the container in
which this was run intercepts TLS, so the dependency-install layers needed a CA shim added to
a scratch copy of each Dockerfile. The wiring and the required-variable results were obtained
*without* it — they fail or succeed before any package is fetched. Only the completed builds
and the `RAMEAU_COMMIT` result required it.

#### DECIDED by delegation 2026-11-02: do NOT adopt the `.CORRECTED` `nginx.conf` variant

John delegated this. The decision rests on the counts already settled above, not on the
filename, and it does not require seeing the file.

| | live variant | `.CORRECTED` |
|---|---|---|
| literal upstreams, resolved at config load | 5 | **8** |
| variable upstreams behind one `resolver`, resolved per request | 3 | **0** |

The two forms fail in opposite directions, and that is the whole decision. A literal
upstream is resolved when nginx **parses** its config: one name that does not resolve and
the container exits or crash-loops, taking the whole site down, `/` included. A variable
upstream behind a `resolver` is resolved **per request**: that route 502s while the SPA keeps
serving.

So adopting `.CORRECTED` would convert three request-time routes into config-load routes —
it moves three failures from *degrade* to *does not start*.

**That is the wrong direction for this release specifically**, and the reason is already
settled in this document: v0.1 defines two services and ships no image for `arbiter`, `kb`,
`broadcaster` or `search`. A portal whose upstreams are all resolved at config load is a
portal that must have every backend present to start at all. Under the live variant the same
absence costs those panels and leaves the site up.

**Not established here, and the decision does not depend on it:** which routes the three
variable upstreams serve. `nginx.conf` is not in this repository. What is established is the
*direction* — three routes move from request-time to config-load — and that is sufficient,
because no assignment of routes makes "cannot start" preferable to "one panel is empty" for
a release that deliberately omits backends.

The candidate is not deleted and nothing is rewritten: it stays unapplied on the private
side. If v0.1 ever defines all of its referenced backends, this decision should be re-taken,
because the argument above expires at that point.

#### DECIDED by delegation 2026-11-02: define none of the six, and remove none of them

Also delegated. Two decisions, because "referenced but undefined" has two possible answers
and the brief was right that defining is not the default.

**Define none.** Per service, with the reason already in `deploy/compose.yaml` rather than
invented here:

| service | why it is not defined |
|---|---|
| `ollama` | inference stays on the host; a container wanting the accelerator needs device passthrough, which ties the stack to one vendor's driver stack. A host-boundary input by design. |
| `embed` | the same boundary: kept separate so embeddings never contend with generation on one accelerator, and `PROVENANCE` records that the private deployment runs it natively rather than in a container. |
| `arbiter`, `kb`, `broadcaster`, `search` | none of their images is part of v0.1, so defining them would describe a stack this repository cannot build. |

Defining any of them would produce a service that comes up and serves nothing — a green
surface over an unrun thing, which is the same defect class as a `?` finding count and as an
empty `skills/`. None is defined.

**Remove none of the references, and this one is a DECLINE with its cost stated.** The
references are not in `compose.yaml`; they are in the compose-form env example generated from
`COMPOSE_SERVICE`. Blanking the six there would be safe-by-construction rather than
safe-by-instruction, which is the better kind — but it was measured against what it costs:

- `test_the_published_ports_do_not_come_back_in_the_compose_example` is a **named regression
  guard**, carrying the exact wrong values that shipped on 2026-10-27 — `:8091`, `:8092`,
  `:11436` among them. Those belong to `kb`, `broadcaster` and `embed`. Blank those lines and
  the guard can no longer fail for the three endpoints it was written to protect. **A guard
  that cannot fire is the defect this repository has spent the most effort removing**, and
  trading one away for a convenience is not a trade this document should record as an
  improvement.
- The generated file serves both cases by design. `compose.yaml` says those endpoints "are
  URLs if you have them and blank if you do not", so `http://kb:8090` is correct guidance for
  an operator who *does* run one on the network, and deleting it serves the operator who does
  not at the expense of the one who does.
- The risk it would remove is already addressed by instruction: `828b6c2` made the generated
  header say, in capitals, that blanking every endpoint you do not run is required rather
  than optional, and it names the six.

What is NOT a reason to decline, recorded so the weak argument is not mistaken for a strong
one: `test_the_compose_example_addresses_the_listen_port` would also stop covering those six,
and that one does not matter — it is circular by construction for exactly these endpoints,
as `PROVENANCE` says of itself, because both sides of the comparison derive from the
registry.

**The obligation this leaves.** The residual risk is an operator who does not read the
header. If step 4 or step 5 shows one actually tripping on it, the fix is to blank the six in
the generator *and* re-anchor that regression guard to something non-circular in the same
change — not to blank them and let the guard go quiet. Named here so the second half cannot
be forgotten.

#### SUSPENDED: the delegated-leaf criterion — that capability is not in the ruled tree

Reported 2026-11-02 by the session that can read the private tree, measured against the
source John ruled into scope on that date, `services/router/`. Nothing here is checkable
from inside this repository; the counts are counts.

```
bifrost_delegate.py in the ruled tree   : NO
igor_tool_loop.py   in the ruled tree   : NO
files mentioning delegate / tool_loop   : 0 / 0
files mentioning subtask / tool_calls   : 0 / 0
files mentioning tools= / "tools"       : 0 / 0
function_call                           : 0
POSITIVE CONTROL -- FastAPI              : 2 files
```

**The positive control is what makes those zeros mean anything.** The same sweep over the
same tree finds FastAPI in two files, so the instrument works and the zeros are absences
rather than a broken search. The ruled router registers 16 routes and no tool or
function-calling surface, with no `add_api_route`, `APIRouter` or `include_router`, so there
is no second registration path hiding one.

So **there is no delegated leaf to exercise.** The delegate exists in the live private
checkout and a July snapshot; it is in neither candidate tree of the release's own source
snapshot.

**What this breaks here.** Measured in this repository: six files instruct a reader to
exercise a delegated leaf, or explain the acceptance criterion in terms of it —
`README.md`, `deploy/README.md`, `deploy/compose.yaml`,
`deploy/images/router/Dockerfile`, `deploy/images/router/Dockerfile.dockerignore`, and this
document. Each is now an instruction that cannot be carried out. **An unsatisfiable
instruction is worse than an open question**, because a reader at step 5 will assume they
have misread it, so each one is marked suspended and points here. The *reasoning* is kept
rather than deleted: it is sound, and it is true of a different artefact.

This is a claim that was true when written and was invalidated by a later ruling, in
documents with no way to notice — the same class as a gate certifying a stale claim.

**And the insufficiency runs the other way now.** The criterion existed because a 200 was
said to prove nothing. Reported module-scope measurement of the ruled tree:

```
main.py module-scope local imports      : classifier, backends (all three),
                                          strategies, commands, metrics
main.py LAZY local imports of those     : 0
lazy imports of the five requirements,
  across the whole ruled tree           : exactly ONE -- `import uvicorn`, inside a
                                          function that cannot run without it
```

Four of the five requirements resolve before the app can answer anything, and the fifth
cannot be absent from a process that is serving. **For the ruled scope, if the container
answers any request at all, every requirement resolved** — so a 200 *is* sufficient for the
manifest question, and the stated reason it is not names a file the artefact does not
contain.

#### The replacement, STAGED AND NOT ADOPTED

If the ruling is that v0.1 ships no delegation, acceptance becomes three legs with distinct
failure modes. Recorded so the work is not re-derived; **not adopted, because the ruling has
not been made.**

1. **The manifest.** Start the container; any 200 discharges all five requirements, for the
   reason measured above. Keep `--no-cache-dir`, as the Dockerfile does.
2. **A real backend call.** `httpx` is imported at startup and never exercised by it, so a
   wrong endpoint URL, a TLS interception or a wrong tier name fails only on a real request.
   `POST /v1/chat/completions` with a trivial prompt, asserting a **non-empty completion**
   rather than a 200 — this router has a measured 200-with-empty-content mode when a
   thinking model's output lands in a side channel, so "200" and "answered" are different
   claims. That distinction is already in `README.md` as a design property; here it is the
   difference between a test and a formality.
3. **The runtime config files.** The four runtime JSON files are read at run time rather than
   imported, which is exactly why CI cannot host this. `GET /classify` exercises the
   classifier against that config and `GET /cascade/{band}` the tier table; a missing or
   unparseable config is invisible to legs 1 and 2.

Leg 1 **replaces** the delegated-leaf test. Legs 2 and 3 are what a 200 genuinely does not
prove, and neither was in the original criterion — so the replacement is not a weakening.

The three routes named in legs 2 and 3 are proposed by the measuring session; this
repository cannot confirm they are among the 16.

#### The ruling this waits on, with the options as reported

It is the `bifrost_delegate.py` scope question, already open above. What is new is that it
now blocks a deliverable and makes live documents wrong rather than leaving a question open.

| option | consequence |
|---|---|
| accept the ruled scope, restate acceptance as the three legs | smallest; the delegate is simply not in v0.1 |
| extend the ruling to include the delegate and tool loop | **this row was wrong and is corrected below (2026-11-03).** It said "entangled sibling tree, four more packages, eight module names it imports without containing"; that came from a stale snapshot. Re-measured: the closure is **self-contained**, the manifest delta is **two** packages, and nothing is imported-but-absent |
| test against the live router | tests the private system, so it cannot discharge the release's acceptance at all |

The measuring session recommends the first, and so do I, for a reason this document already
holds: the release's acceptance must be discharged by the release's artefact. The third
option fails that on its face, and the second reopens a closure that was verified in both
directions. **Recorded; none adopted.**

#### A second claim the same measurement strands: `/livez`

`livez` and `readyz` both count 0 in the ruled tree, yet this repository asserts in several
places that the application serves `/livez` for liveness. Measured here, it appears in:
`shared/resolver.py`'s `router_health` comment, the failure message of
`test_the_health_probe_target_is_not_the_unfalsifiable_route`, `README.md`, and
`deploy/README.md`'s acceptance procedure, which curls it.

The test does not break — it asserts the probe target is `/health`, which is correct and
unaffected — but its message repeats the claim, so the inventory is recorded here rather
than churned now. The primary claims are marked; the correct remedy depends on the same
ruling, because a tree that gains the delegate may gain `/livez` with it.

#### KNOWN AND NOT RULED: two private-range strings remain reachable in this history

`b465f4a` rebased two redaction-mask test fixtures off the private /24 and onto TEST-NET-1.
The tip is clean and a sweep confirms it. The residue behind it is larger than one commit,
and it is in **two different kinds of object**, which is the part that matters for anyone
costing a remedy. Measured on `main` at `d28dc87`, counts only:

| object | surface |
|---|---|
| the tip's tree | 0 |
| trees | **20 of 30 commits**, `eb31a16` (where it entered) through `f31be0c` |
| commit **messages** | **2 commits, 4 lines** — `b465f4a` (3) and `9c96ab1` (1) |

**A rewrite scoped to blobs would leave the messages untouched and would read as
complete.** That is the trap: the tree surface is the one a filter finds, and the message
surface is the one that then survives, so a remedy has two kinds of object to reach rather
than one.

`b465f4a` is the commit that removed the strings, and its message quotes them because it
explained the rebase by showing before-and-after pairs — which necessarily republished the
private form in the course of recording its removal. `9c96ab1` predates that work and was
not previously counted.

Recorded as a decision rather than a non-issue, with the reasoning on both sides, because
each side is true:

- **It grants little.** The range is RFC 1918, non-routable and in use on a very large number
  of networks, so knowing it identifies nothing and reaches nothing.
- **It violates this repository's own stated rule**, the first of the rules below: no address
  from the private deployment appears here. A rule that is suspended once it becomes
  inconvenient is not a rule, and the whole release is organised around that one.

And unlike a credential, **an address cannot be rotated** — the remedy that this document
prescribes for a leaked secret has no analogue. The rule immediately below also says a
rewrite does not un-send what has already left the machine it was on, which cuts against
rewriting history to fix it: the rewrite would change what a future clone sees without
changing what has already been distributed.

So: **do not rewrite history on this.** It is John's to rule on, it is recorded here so it is
not rediscovered as a new finding, and the honest position in the meantime is that the tip
obeys the rule and the history does not.

#### DECIDED by delegation 2026-11-02: do NOT restructure the repository for step 4

Delegated with three constraints that were not mine to relax. The decision is to change no
paths, and it is a decision rather than inaction: the obvious restructuring was considered
and is worse than what is here.

**The candidate was moving each Dockerfile into its own build context** — `services/<name>/`
— which would end the Dockerfile-outside-its-context wiring, let each ignore file be a plain
`.dockerignore` at a context root, and remove the `../../` in `compose.yaml`. Rejected for
three reasons, in order of weight:

1. **It would put the release's reviewed packaging inside the directory step 4 assembles.**
   The assembly would then have to avoid overwriting two files it is otherwise free to fill,
   and the packaging would stop being reviewable independently of the source. `deploy/` holds
   the packaging and `services/` holds the assembled tree; that separation is what lets the
   Dockerfiles be reviewed before the source they will build exists, which is the position
   this release is actually in.
2. **There is no defect to fix.** The wiring was exercised on 2026-11-02 through
   `docker compose build` — both build definitions loaded, both contexts transferred, in one
   invocation. It is the part `compose.yaml` itself calls most likely to be wrong at step 5,
   and it is now the part that has been run.
3. **It would require moving each ignore file with its Dockerfile and re-proving the bound**,
   which is one of the three constraints. Paying that cost to fix nothing is the wrong trade.

**The `skills/` obligation cannot be fixed by any layout.** The directory one Dockerfile
needs and the manifest it needs were measured in different private trees. That is a fact
about the sources, not about where files sit here, so no restructuring makes the assembly a
copy of one directory.

**What was actually missing, and is now written instead.** Four files decided what each
context must contain — two Dockerfiles and the two ignore files beside them — so an
assembler had four places to reconcile and no single statement of the target.
`deploy/README.md` now carries that layout once, with the deny-then-readmit list, the
verification commands,
and one precondition worth repeating here because it is silent in both directions:

> `COPY *.py ./` is top level only, and the ignore file's `!*.py` is top level only too. A
> router module in any directory other than `backends/` or `skills/` is therefore dropped
> **twice** — excluded by the ignore file and named by no `COPY` — with no build-time error.
> It fails at import, inside the container, after a green build. The layout assumes the
> router's modules are flat; the ruled tree is consistent with that, but that is one
> snapshot rather than a guarantee, so it is a precondition to check at step 4. Adding a
> third package directory needs a `COPY` group and an ignore-file admission **together**;
> adding one without the other is silent either way.

## Rules that outlive the release

- **No credential, key, token, address or hostname from the private deployment appears in
  this repository.** Configuration is described by example files with placeholder values.
- **The private archive's history is never published**, in whole or in part, and is never
  this repository's history.
- **No claim in the README or in release material that the code here does not support.**
- **Verification is never self-reported.** A change is reviewed by something other than
  whatever produced it.
- **Describe a redaction by its SHAPE, never by quoting the before-and-after literal.** A
  commit, comment or document that records the removal of a value by showing the value
  republishes it, in an object that outlives the removal — and a message is not reached by
  a rewrite aimed at trees. "A last-octet mask became a documentation-range mask" carries
  everything a reader needs; the pair of literals carries the value as well. This is the
  rule that `b465f4a` broke while obeying the first one, which is why it is written here
  and not left as a lesson in a commit nobody will re-read.

## The gate audit of 2026-11-03, and what it found

Every check in `.github/workflows/ci.yml` and `tools/` was read against two questions, and
**nothing was changed while reading**: a sweep that fixes as it goes cannot afterwards say
how many checks were sound, and that number is the point of the exercise.

1. Is a non-zero exit anywhere read as a **verdict**, when it could also be a **crash**?
2. Does a control exercise only a **subset** of its subject's arguments, inputs or
   environment — so that it cannot fail the way its subject fails?

**23 checks audited: 13 sound, 1 weak, 9 carrying a defect.** Nine distinct defects, one of
which (D) accounts for four of those nine rows.

The result is lopsided and the shape of it matters more than the count: **every defect is in
`ci.yml`, and the hook is clean.** `tools/git-hooks/pre-push` was rewritten on 2026-11-02 for
exactly this class of fault. `ci.yml` carries the same control, written the same way, and was
not revisited — so the fix landed on one of two instances of one defect. That is the
generalisable lesson here, and it is why this audit was asked for as a sweep rather than as a
second look at the hook.

### The nine findings

**A — `ci.yml`'s canary reads a crash as a fired canary.** `prove the scanner fires` treats
any non-zero exit as "the planted credential was flagged". Measured: gitleaks exits **1** both
when it finds a secret and when it cannot write its report, so the two are indistinguishable
from the exit code. A stub scanner that finds nothing and exits 1 makes the step print
`control ok: gitleaks flags a generated known-positive` and exit 0. The same stub makes the
hook refuse, by name, with the reason. Proposal: classify by report shape, as the hook does.

**B — that canary runs a subset of its subject's arguments.** The gate passes
`--report-format sarif --report-path`; the control passes neither. This is verbatim the
asymmetry the hook's own comment records as having been worse than useless. Proposal: give
the control the gate's reporting arguments. A and B are one row and one fix.

**C — a step named for coverage it does not have.** `scan working tree and full history`
runs `gitleaks detect --source`, which traverses **history only**. Measured: an uncommitted
credential exits 0 under `--source` and 1 under `--no-git`. On a clean CI checkout the
working tree is a subset of history, so the **effect** is correct and nothing is unscanned —
but the name asserts a check that did not run, which is the defect the hook was corrected for
in the same breath. Proposal: rename. Do not add `--no-git` to a gate; see H7 below.

**D — four skip conditions that a broken checkout satisfies.** `byte-compile python, if any`,
`run tests, if any`, `the example env files must be GENERATED` and `the in-network profile
must not contain loopback` each skip, green, when they cannot find their subject, with no
presence assertion first. This is the `validate compose` defect of 2026-10-27, which printed
`no compose file yet - skipping` while a syntax error shipped. It is now **strictly** a
false-green path, because the tree does contain python, 87 tests, a resolver and a profile: in
a healthy tree those branches are unreachable, so reaching one means something is wrong and
the step says "expected pre-source". Proposal: the presence guard the `images` job already
uses — assert what cannot be absent, then skip only on what legitimately can.

**E — `if grep` reads grep's error as a clean result.** The loopback check. Measured: grep
exits **2** on an error and `if grep` treats that as no match, so an unreadable profile
reports `no loopback in the in-network profile`. Proposal: branch on the three statuses.

**F — a control whose comment claims more than its loop checks.** `profiles/compose.env must
actually reach the router` says "the three router URLs are hardcoded in compose.yaml ON
PURPOSE and MUST keep overriding the file" and then iterates **two**, omitting
`RAMEAU_ROUTER_CHAT_URL`. `compose.yaml` hardcodes all three. Severity is genuinely low —
`shared/test_resolver.py` asserts the omitted one — so the defect is the overclaim, not a
coverage hole. Proposal: add the third to the loop.

**G — `|| true` swallowing a read error, in the check added for this very class.** `every
script must be executable IN THE INDEX` does `head -c 2 -- "$f" 2>/dev/null || true`.
Measured as an unprivileged user: an unreadable file passes `[ -f ]`, `head` fails, the
substitution is empty, the shebang test is false, and the file is **skipped silently** — and
not counted, so the `scanned -eq 0` backstop does not see it either. This is mine, and it is
the same `|| echo '?'` shape that was removed from the hook two days earlier. Proposal:
distinguish "not a script" from "could not be read", and fail on the second.

**H — that check has no control.** Nothing plants a `100644` file with a shebang and confirms
it is flagged. The `scanned -eq 0` assertion proves the loop read *something*, which is a
partial control and not the same thing. Recorded as an obligation rather than a defect: the
check is correct today by inspection, and an uncontrolled check is weaker than a controlled
one without being wrong.

**I — `export` masking a failed command substitution.** `build both images` runs
`export RAMEAU_COMMIT="$(git rev-parse --short HEAD)"`. Measured: `export V="$(false)"` exits
**0** under `set -euo pipefail` and leaves `V` empty, where the bare assignment `V="$(false)"`
exits 1. So a failed `rev-parse` yields an empty value, `${RAMEAU_COMMIT:-unknown}` resolves
to `unknown`, and the image is labelled with a plausible wrong provenance instead of the build
failing. Proposal: assign, then export.

### The one weak check, recorded as weak rather than as a defect

`validate compose`'s required-variable control asserts that the stack refuses to resolve with
`RAMEAU_OLLAMA_URL` unset, by treating any non-zero `docker compose config` as confirmation.
Measured: it prints `control ok` for a compose file that does not mention the variable and does
not parse. But it correctly **fails** for a parseable file that has no required variable —
which is the direction the control exists to protect — and the gate on the next line catches
the unparseable case, so there is no green anywhere. It certifies for the wrong reason without
certifying a falsehood. Proposal, optional: match the interpolation error text.

### Two things this audit deliberately does not propose

- **The advisory working-tree pass stays advisory.** `--no-git` reports untracked *and*
  gitignored files, so a local `.env` — the file `.gitignore` exists to keep out — would
  refuse every push. A gate people disable protects nothing. It has no control, and an
  advisory pass needs none; it is the only check here whose correct form is "warn and
  proceed", and it must not be promoted on the strength of appearing in a defect list.
- **The hook scanning all local history rather than the pushed refs.** It ignores the ref
  information on stdin, so a finding on an unpushed local branch refuses the push. That
  over-blocks, which is the safe direction, and narrowing it would trade a false refusal for
  a possible miss. Left as it is, named so it is not rediscovered as a defect.

### The obligation this leaves

The nine fixes are **not in this change**, by instruction: the audit is the deliverable and a
verdict per check is only meaningful if it predates the edits. They land next, A and B
together, and each one carries the measurement recorded above rather than a fresh assertion.

### All nine discharged, 2026-11-03

Every finding is fixed, and each fix was shown to FAIL before being called done. Nine of the
ten arms below run the OLD step from `main` against the same condition, because a fix whose
defect cannot be reproduced on demand has not been shown to fix anything.

| # | arm | old step | new step |
|---|---|---|---|
| A | crashing scanner, no report written | `control ok …`, exit **0** | refuses: "crashed rather than found the canary" |
| A | scanner misses the canary (exit 0) | fails | fails: "did not flag a generated known-positive" |
| B | — | control passed neither reporting argument | control passes the gate's `--report-format sarif --report-path` |
| C | — | *no behavioural arm: this is a rename* | step now named `scan every object in full history` |
| D | each of the four subjects deleted | skips green, "expected pre-source" | fails, naming the file |
| E | profile unreadable (`grep` exit 2) | `no loopback …`, exit **0** | fails: "an ERROR rather than a verdict" |
| F | `RAMEAU_ROUTER_CHAT_URL` pointed at a wrong host | passes, exit **0** | fails, naming the variable |
| G | two tracked scripts, one unreadable | `scripts checked: 1`, `ok: every tracked script is executable`, exit **0** | fails, naming the unreadable file |
| H | `scan_modes` mutated so it cannot flag a mode | *no control existed* | its own control fails: "expected exactly 'not-executable.sh'" |
| I | `git rev-parse` fails | exit **0**, `RAMEAU_COMMIT` empty | exit 128 at the assignment |

Three things the arms changed about the audit's own conclusions, recorded because a finding
that survives testing unaltered is the exception rather than the rule:

- **G is less severe than the audit said, and more severe later.** With the hook as the only
  shebang file, the `scanned -eq 0` backstop catches an unreadable one by luck. Measured with a
  second script added: the old step prints `scripts checked: 1` and `ok: every tracked script
  is executable in the index` and exits 0 — a success report about a file it never read. So the
  defect is latent today and real from step 4, when more scripts arrive.
- **Two of my own D fixes carried a guard that could not fire.** The first version asserted an
  anchor and *then* checked for a contradiction between the anchor and `find`; since the anchor
  is itself a `.py`, `find` always matches it and the branch was dead. Removed rather than
  shipped — a guard incapable of failing is the defect this change is about. Found by running
  the arm, not by reading the diff.
- **Two step names were also wrong, by finding C's own argument.** `byte-compile python, if
  any` and `run tests, if any` no longer have an "if any": they assert their subject. Renamed,
  and the workflow header's claim that the build steps "pass on an empty tree" is now scoped to
  the `images` job, which is the only one whose subject legitimately has not arrived.

Not changed, deliberately: the advisory working-tree pass, the hook's history scope, and
`validate compose`'s weak control, each for the reason recorded above.

## The step-4 preflight, 2026-11-03

Everything above this line that places an obligation on step 4 was enforced by nothing. Each
was measured, each is correct, and each depended on a human reading a 900-line document at
the one moment it matters. That is the weaker half of a gate — the same shape as the
executable bit before `28f7c82`, generalised to every other obligation in this document.

`.github/workflows/ci.yml` now carries a `step-4 preflight` job that asserts them in one
place and reports exactly one state by name. The executable-bit check is **folded into it**
rather than left standing in `build`, so one job reports one state; **Finding G of the gate
audit is therefore discharged by this job and is no longer separately latent.**

| state | meaning | result |
|---|---|---|
| `NOT ASSEMBLED YET` | `services/` absent; repository obligations hold | pass |
| `NOT ASSEMBLED YET, AND VIOLATED` | `services/` absent, but a repository obligation fails **now** | fail |
| `ASSEMBLED AND CLEAN` | `services/` present, every assertable obligation holds | pass |
| `ASSEMBLED AND VIOLATED` | `services/` present, at least one fails | fail |

The fourth name is a departure from the three the brief asked for, and the reason is a defect
the arms caught in the first version: with only three states the job failed solely on
`ASSEMBLED AND VIOLATED`, so a tracked GPL `.jar`, a model weight, a `100644` script or a
displaced ignore file was **listed as a violation and then exited 0**. The job printed its
findings and waved them through — the shape `2f30729` corrected in the hook. Three states are
right about *assembly*; they do not cover a live defect found before step 4.

**What it asserts.** Five hold with or without the source: the ignore files sit where Docker
reads them (**the location, not mere existence** — a rename or a lone `git mv` disarms every
pattern silently); no `.jar`, because a GPLv3 artefact in an Apache-2.0 release is a licence
conflict; no model weights; `NOTICE` exists the moment any font is tracked, which couples two
things this document could previously only ask a reader to remember; and every tracked
shebang file is `100755` in the index, with its control.

Seven more need the assembled source and are **named and reported DEFERRED** until it exists,
never counted as satisfied: every router and portal `COPY` source present including
`nginx.conf`; `skills/` non-empty and still Markdown-only; `backends/` non-empty;
`uvicorn[standard]` rather than bare `uvicorn`; nothing credential-shaped anywhere in either
context; and **the flat-modules precondition**, which is the one worth having — a router
module outside `backends/` or `skills/` is dropped twice, by the ignore file and by no `COPY`
naming it, and fails at import after a green build.

An eighth item, the size the ignore files hold back, **reports rather than asserts**. There is
no byte ceiling, because this repository cannot derive one and an invented figure is what the
withdrawn "131" was. Enforcement is the location assertion plus the `images` job, which
transfers the context for real.

**Three obligations it cannot assert, which therefore belong to step 5 and are named in its
own output** rather than passed over: the four runtime JSON files, absent by design and
unhostable in CI; `docker/.env.example`, which must be read line by line because it entered
the release set on its filename alone; and the acceptance procedure itself, whose
delegated-leaf step is suspended below.

**One thing the preflight cannot fix, recorded instead.** "No stub survived" is not directly
assertable: nothing distinguishes a stub from assembled source by inspection. What the job
does instead is make the distinction irrelevant — a `services/` that satisfies all seven
source obligations is not a stub in any respect the build cares about. Relatedly, the
protection that kept stubs out of this repository during development was
`.git/info/exclude`, which is **local and does not travel**; `.gitignore` carries no
`services/` rule deliberately, because a rule that must be *removed* at step 4 is the same
hazard as an obligation that must be remembered.

## The delegation's real shape, re-measured 2026-11-03 — NOTHING ADOPTED

Reported by the session that can read the private tree. Counts and line counts only; no
source, no values, and the two module names were already public in this document.

```
bifrost_delegate.py                  774 lines
igor_tool_loop.py                  2,141 lines
local closure reachable from them     10 modules, ~5,360 lines
imported-but-absent modules            0      <- SELF-CONTAINED
new third-party requirements            2      fastmcp, mcp
already in the release manifest         3      httpx, pydantic, prometheus_client
tests referencing the mechanism        23
```

**The zero is the figure that matters.** The options table above said this path pulls in an
entangled sibling tree, four more packages and eight modules imported without being present.
That was read off a stale snapshot and is wrong about what actually runs: a port would be a
copy of ten self-contained modules plus two public PyPI requirements, not a rewrite. The
engineering question is substantially smaller than this document implied, which is why the
row is corrected rather than left to be rediscovered.

**And the real constraint is not engineering at all.** Both seed modules are rule-1 protected
on the private side, so a port publishes protected internals. It is a **disclosure** decision,
not a difficulty one — still John's, but for a different reason than this document gave. A
reader who concluded "too entangled to be worth it" would have been reasoning from a figure
that no longer holds.

### What the release would have to do to accept it, so the port is mechanical

Answerable now, without the code:

- **Manifest delta: two lines.** `fastmcp` and `mcp`; the other three are already in. The
  form is not decided here because floors-versus-pins is still John's. Both names must be
  read from `importlib.metadata.packages_distributions()` in an environment where they are
  installed — never from recall — which is the standing rule for this manifest and not a
  comment on these two packages.
- **The router ignore file: possibly nothing, and the preflight now says which.** It denies
  `**` and re-admits named paths, and `!*.py` already covers any module landing flat. A new
  package directory needs a `COPY` group **and** a `!dir/` plus `!dir/**` admission
  *together* — and those `!` lines must go in the re-admit block, **before** the
  never-under-any-circumstances block, because an ignore file takes the **last** matching
  pattern and `.env`, `*.pem`, `*.key`, `.netrc`, `.git-credentials` must keep winning. The
  flat-modules assertion in the preflight is exactly the question this turns on, so after
  step 4 the answer is measured rather than assumed.
- **The 23 tests: into the repository, not into the image.** The ignore file denies `tests/`,
  `test_*.py` and `*_test.py`, so they cannot reach the build context by design, and the
  repository already tracks a test suite. Their own test-time dependencies are not among the
  figures above and are unmeasured here.
- **Acceptance: leg 1 changes, legs 2 and 3 do not.** Leg 1 rests on every requirement
  resolving at module scope before the app can answer. `bifrost_delegate.py` is read off disk
  with `ast` and imported by nothing, so `fastmcp` and `mcp` would be reached only when a
  delegated leaf actually runs — and a 200 would stop discharging the manifest. The
  consequence is precise: **the suspended delegated-leaf criterion un-suspends rather than
  being replaced**, which is why it was suspended and not deleted. Legs 2 and 3 test a real
  completion and the runtime config, neither of which this touches.

**Nothing here is adopted.** The ruling is John's, the recommendation in the options table is
unchanged, and the only thing that has changed is that one of its three rows was wrong.

## Dispositions carried forward, 2026-11-03

- **Finding G** — discharged by the step-4 preflight, which absorbed the executable-bit
  check. Not separately latent any more.
- **The weak check stays weak.** `validate compose`'s required-variable control still
  certifies for the wrong reason, the preflight does not touch it, and its label is
  unchanged. It was not relabelled on the strength of unrelated work landing.
- **Fifteen merged branches** cannot be deleted from this session: the device-trust check
  refuses it. Named once, needing no measurement, and not this document's to resolve.
