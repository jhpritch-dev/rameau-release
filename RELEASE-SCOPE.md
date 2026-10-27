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

## Explicitly NOT in v0.1

Listed, not forgotten. Each is either host-specific, depends on infrastructure that is not
being published, or is large enough to deserve its own release.

| cut | reason |
|---|---|
| overnight orchestration / scheduled self-improvement | operationally entangled with a specific fleet |
| the agent sandbox | depends on a Linux-only isolation mechanism not present on every host |
| NPU classification path | hardware-specific, and the measured failure rate on the one host that has it is too high to publish |
| quantum-related experiments | research, not product |
| workflow-automation integration | third-party service wiring, host-specific |
| messaging bridges and notification transports | credential-bearing by nature |
| the portal redesign | its own track, with its own design review |
| GitOps / continuous-deploy layer | a ship-path concern, not a routing concern |
| multi-host fleet management | the private deployment's shape is not the product |
| benchmark harness and published throughput figures | figures belong to hardware; publishing them would invite machine-to-machine comparison, which this project explicitly does not do |

If a week of the release schedule slips, the slack is taken from the bottom of the in-scope
table — never from the scrub or the scanning steps.

## Known assembly obligations, recorded before they can be forgotten

From the allowlist census of 2026-10-27 (286 of 7,009 source paths selected, 3.4 MB):

- **A `NOTICE` file is required at the root.** Nine `.woff2` faces (Instrument Sans,
  Instrument Serif, JetBrains Mono) and their three OFL licence texts are in the release
  set. SIL OFL 1.1 is compatible with Apache-2.0 but is *not* Apache-2.0, so the third-party
  attribution has to be stated rather than implied. This file does not exist yet because the
  fonts have not landed; it goes in with them, not before.
- **`docker/.env.example` must be read line by line before it is copied.** It was classified
  into the release set on its *filename* alone. A `.example` suffix is a convention, not a
  guarantee, and this is exactly the kind of file where the convention is most often broken.
- **`shared/resolver.py` does not exist in the source archive.** It is the keystone of the
  literal-and-address resolver sweep, so the sweep cannot begin by running it — it has to be
  written first. Treating it as existing would have made the sweep look like a mechanical
  pass when it is partly new work.
- **Only 72 of the 286 selected paths are a safe byte copy.** 199 need the resolver sweep, 7
  must be derived as `.example` files, and 8 need documentation scrubbing. The assembly is a
  transform, not a copy, and the per-path transform is recorded in the census.
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
