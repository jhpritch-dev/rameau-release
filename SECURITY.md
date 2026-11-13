# Security policy

## Reporting a vulnerability

Please report security issues through GitHub's private vulnerability reporting on this
repository (Security → Report a vulnerability). That channel is private and does not create
a public issue.

Please do not open a public issue for a suspected vulnerability, and please do not include a
working exploit against a third party's deployment.

Expect an acknowledgment within a week. This is a small project with one maintainer; a
realistic timeline is stated rather than an aspirational one.

## Supported versions

Nothing is supported yet. v0.1 is not tagged. Once it is, the most recent minor version is
the only supported one.

## Scope

In scope:

- the routing layer, its classifier, its cache and its cascade logic
- the deployment artefacts in this repository, including the Compose stack
- any credential, key or internal address that has reached this repository (see below)

Out of scope:

- the model servers RAMEAU routes to; report those upstream
- the private deployment this release was derived from, which is not published
- denial of service achieved by sending a local model more work than its hardware can do

## If you find a credential in this repository

Report it through the private channel above. It would be a real finding and it would be
treated as one.

This repository is assembled from an allowlisted, credential-scrubbed subset of a private
archive, with three independent layers meant to stop that: the allowlist decides what is
copied at all, `.gitignore` blocks credential-shaped paths from being staged, and CI runs a
secret scan on every push and pull request. A credential reaching `main` means all three
failed, so the report is useful well beyond the single value.

Note also that rotation, not deletion, is the remediation for an exposed credential. A
commit that removes a value does not make the value safe — it only makes it harder to find.
Any report will be answered on that basis.

## What this project does not do

- It does not phone home. There is no telemetry endpoint.
- It does not require an outbound network path in its offline deployment profile.
- It does not store request or response content beyond what its configured logging states.
