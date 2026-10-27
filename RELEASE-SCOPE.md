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

## Rules that outlive the release

- **No credential, key, token, address or hostname from the private deployment appears in
  this repository.** Configuration is described by example files with placeholder values.
- **The private archive's history is never published**, in whole or in part, and is never
  this repository's history.
- **No claim in the README or in release material that the code here does not support.**
- **Verification is never self-reported.** A change is reviewed by something other than
  whatever produced it.
