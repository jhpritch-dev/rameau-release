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

## Rules that outlive the release

- **No credential, key, token, address or hostname from the private deployment appears in
  this repository.** Configuration is described by example files with placeholder values.
- **The private archive's history is never published**, in whole or in part, and is never
  this repository's history.
- **No claim in the README or in release material that the code here does not support.**
- **Verification is never self-reported.** A change is reviewed by something other than
  whatever produced it.
