# Working in this repository

Read this before doing anything else here. It is short on purpose.

## What this repository is right now

Scaffolding. Licence, ignore rules, secret-scanning CI, scope document. **There is no source
tree yet.** It arrives as an allowlisted, credential-scrubbed subset of a private archive,
and only after the scrub reads clean under more than one scanner. See
[RELEASE-SCOPE.md](RELEASE-SCOPE.md) for the assembly order.

If you were expecting source and did not find it, that is the current state and not a
checkout problem.

## The one rule that matters most

**Never commit a credential, key, token, password, private address or internal hostname.**

Configuration is described only through example files with placeholder values. If a task
seems to require a real value, the task is wrong — say so instead of inventing or embedding
one. CI runs a secret scan on every push and pull request; treat a finding as a stop, not a
lint warning to silence.

This matters more than usual here. A value committed to a private repository has already
left the machine it came from, and a later rewrite does not un-send it. Rotation, not
deletion, is the remediation — so the cheap moment to get this right is before the commit.

### Install the pre-push gate

```
git config core.hooksPath tools/git-hooks
```

GitHub's push protection, which blocks a credential *before* the object reaches the remote,
is **not available on this repository while it is private** on the current plan — secret
scanning returns 422 and rulesets return 403. Both only switch on when the repository goes
public, which is the inverse of when they are most needed: the private window is exactly
when the source tree lands.

So CI is the only remote gate, and CI runs *after* the push has landed. The hook in
`tools/git-hooks/pre-push` restores the block-before-it-leaves property locally. It **fails
closed**: with gitleaks absent it refuses the push rather than waving it through, because a
gate that silently disables itself when its tool is missing is worse than no gate — it
manufactures the appearance of protection.

## What this environment can and cannot reach

This session runs against the repository only. **It has no network path to the deployment
RAMEAU was derived from**, and that is by design rather than a limitation to work around.

So:

- Do not write code that assumes a reachable private host, and do not hardcode an address
  to make something testable. Use a configurable endpoint with a placeholder default.
- Do not try to verify behaviour against a live model host. Verify against tests, fixtures
  and a locally runnable CPU-sized model if one is configured.
- Anything that genuinely needs the real deployment is not this session's work. Say what
  needs measuring and leave it.

## Division of labour

This session's job is **judgment**: security review of the tree, adversarial review of
changes, Docker and Compose design review, documentation prose, and the final pre-release
scan.

It is **not** this session's job to author the bulk of the refactor, and it must **never
verify its own edits**. A change and its review come from different places. If you are asked
to both make and approve a change, split it or decline the second half.

That is not ceremony. A reviewer that reads its own work finds what it was already looking
for.

## Honesty rules, applied to code and to prose

- **No claim in the README, docs or release notes that the code here does not support.** If a
  feature is cut, RELEASE-SCOPE.md lists it as cut; add to that list rather than quietly
  softening a claim.
- **A measured figure carries the configuration it was measured on, or it is not quoted.**
  Do not reproduce a throughput or latency number without its basis.
- **Never compare one machine against another.** This project treats hardware agnosticism as
  a property to preserve; a machine-to-machine comparison would undermine it.
- If a test is failing, say it is failing and show the output. Do not describe a partial
  result as a pass.

## Conventions

- Licence is Apache-2.0. New source files carry the standard short header.
- Python targets 3.11+. Keep output ASCII-safe where it is logged.
- Shell and Compose examples must work on Linux; do not assume a Windows host.
- Keep the offline deployment profile genuinely offline — no new outbound dependency may be
  added to it without saying so explicitly in the pull request.

## Before you open a pull request

1. CI green, including the secret scan.
2. Nothing added to `.gitignore` purely to make a scan pass.
3. The diff contains no address, hostname, key or token.
4. Scope: if the change touches something RELEASE-SCOPE.md lists as cut, stop and ask.
