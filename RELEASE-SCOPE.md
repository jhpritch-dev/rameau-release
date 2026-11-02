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
