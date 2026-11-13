# Remote-ref record: `claude/dazzling-gates-*` tips, recorded before deletion

Measured live with `git ls-remote --heads origin` at **2026-11-09T17:22:03Z** (UTC).
Remote: `git@github.com:jhpritch-dev/rameau-release.git` (public).

## Scope of the measurement

- Heads on origin at that moment: **24**. This is the live number; it can grow.
- Selected by **exact string prefix** `refs/heads/claude/dazzling-gates-`: **19**.
- Not selected, left untouched: **5**, including `main` at `2b1722bc884ddc3d7da0dd5b8b880155c52a1f0f`.
- Tags on origin: **none** (`git ls-remote --tags origin` returned empty).

Selection was by exact prefix test in code, not by a shell glob, and asserted both
ways: every selected ref starts with the prefix, no kept ref does, and `main` is in
the kept set. Every selected ref is also inside the `refs/heads/claude/` namespace.

## Why this file exists

Deleting a remote branch ref is not reversible through git, and **none of these 19
tips is an ancestor of `main`** -- each carries 1 or 2 commits that `main` does not
contain. So `main` is not a safety net here and this record is.

A SHA written in a file is only a handle while the object survives. Each tip is
therefore also pinned locally in this clone as
`refs/recovered/claude-dazzling-gates/<leaf>`, which keeps the objects reachable
through `git fetch --prune` and `git gc`. Measured: **333 objects** reachable from
the 19 pinned refs, **0 missing** under `git cat-file --batch-check`. Positive
control: the same check reports `missing` for `dead0000...dead0000`, so the zero is
a real reading and not a silent instrument.

To restore one branch to the remote:

```
git push origin <tip-sha>:refs/heads/<branch>
```

To list the local pins:

```
git for-each-ref --format='%(objectname) %(refname)' refs/recovered/
```

## The 19 deleted remote refs

### 1. `claude/dazzling-gates-b32c16`

- tip: `8ebfb59142c7aa519f88b5b821c3b8c4bd7ae88f`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16`
- commits:
    - 8ebfb59 deploy: four honest config placeholders, a CI image gate, and three Dockerfile findings

### 2. `claude/dazzling-gates-b32c16-blank`

- tip: `751e7962e020e3c68d84131dcbca26e4795d1515`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-blank`
- commits:
    - 751e796 resolver: say "blank what you do not run" in the file the operator actually edits

### 3. `claude/dazzling-gates-b32c16-census`

- tip: `b485a61703e6f13175987efc4c4d4a6fc352cd7c`
- ancestor of `main`: **no**
- commits not in `main`: **2**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-census`
- commits:
    - b485a61 ci: the arms job must install what the arms drive, and the harness must say so
    - 329d23b ci: derive the matcher population from the source, and commit every arm

### 4. `claude/dazzling-gates-b32c16-ciguards`

- tip: `54cc1fce715bd927b5463df363259c15e2ea1dda`
- ancestor of `main`: **no**
- commits not in `main`: **2**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-ciguards`
- commits:
    - 54cc1fc ci: upload-artifact is a Node 20 action too, which only the first run revealed
    - 0f47d8a portal: refuse a silent npm ci, and move off the deprecated Node 20 actions

### 5. `claude/dazzling-gates-b32c16-dockerignore`

- tip: `030961c4e082f5c3cb04d57b56ffcea21632f103`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-dockerignore`
- commits:
    - 030961c deploy: name both ignore-files for their Dockerfile, so Docker actually reads them

### 6. `claude/dazzling-gates-b32c16-envfile`

- tip: `852123abf47009d04496af397f44f82704b890fd`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-envfile`
- commits:
    - 852123a compose: let profiles/compose.env actually reach the router

### 7. `claude/dazzling-gates-b32c16-fixes`

- tip: `5afacb15577a72e02d380d9beb9291c073a0c992`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-fixes`
- commits:
    - 5afacb1 ci: fix all nine gate defects, each shown to fail before being called fixed

### 8. `claude/dazzling-gates-b32c16-hook-exec`

- tip: `e6286f0dedc433d7812047c3ee4ca190bead032d`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-hook-exec`
- commits:
    - e6286f0 tools: make the pre-push gate executable, because git was silently ignoring it

### 9. `claude/dazzling-gates-b32c16-hookfix`

- tip: `b4fd3396ba5ecec6f63795cf3463c0316a37582f`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-hookfix`
- commits:
    - b4fd339 pre-push: stop reporting a crash as a verdict, and make the control able to fail that way

### 10. `claude/dazzling-gates-b32c16-item1`

- tip: `ba6c329b89d125f590c311ebdee8f45992318501`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-item1`
- commits:
    - ba6c329 RELEASE-SCOPE: decide the nginx variant and the six services, both delegated

### 11. `claude/dazzling-gates-b32c16-item2`

- tip: `87588c95c8a1c62e62c59a5bfd3a429b72b85f43`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-item2`
- commits:
    - 87588c9 deploy: decide against restructuring, and write the target layout step 4 must produce

### 12. `claude/dazzling-gates-b32c16-item3`

- tip: `62136a924a1ebf1984946d266202c6a1e08a00b6`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-item3`
- commits:
    - 62136a9 RELEASE-SCOPE: audit every gate for crash-as-verdict and subset controls

### 13. `claude/dazzling-gates-b32c16-leaf`

- tip: `230f6f36c29ac8f38c9d257791de2df7c7ba1d23`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-leaf`
- commits:
    - 230f6f3 docs: suspend the delegated-leaf criterion -- that capability is not in the ruled artefact

### 14. `claude/dazzling-gates-b32c16-manifest`

- tip: `06ff0e82540848fa0e7ec657b272feb08fdfb33b`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-manifest`
- commits:
    - 06ff0e8 docs: withdraw the "131 dependency decisions" figure, and record what the manifest needs

### 15. `claude/dazzling-gates-b32c16-matchers`

- tip: `f6e50e8e59aef11e5fe8885cbefe99eca851e5d6`
- ancestor of `main`: **no**
- commits not in `main`: **2**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-matchers`
- commits:
    - f6e50e8 RELEASE-SCOPE: correct the matcher denominator -- 21, not 12
    - 06ca054 ci: plant a positive and a near-miss against every matcher, and fix the hole it found

### 16. `claude/dazzling-gates-b32c16-modes`

- tip: `93fd43b2429547e6a92a3c919a737dcad600773a`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-modes`
- commits:
    - 93fd43b ci: enforce the executable-bit obligation, and write the step-5 acceptance procedure

### 17. `claude/dazzling-gates-b32c16-preflight`

- tip: `1da90a89a2eb25134a651194a709300a6ac46336`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-preflight`
- commits:
    - 1da90a8 ci: one preflight that asserts every step-4 obligation, and fires at the transition

### 18. `claude/dazzling-gates-b32c16-ruling`

- tip: `ecef3f6953aebc4b0d79c8532036e3a4f6cb791c`
- ancestor of `main`: **no**
- commits not in `main`: **1**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-ruling`
- commits:
    - ecef3f6 RELEASE-SCOPE: record John's ruling and the verified closure, and NOT place the manifest

### 19. `claude/dazzling-gates-b32c16-scope`

- tip: `b8a7e58a7df1a01725c7ab5c0a905f0a12921b2f`
- ancestor of `main`: **no**
- commits not in `main`: **2**
- local pin: `refs/recovered/claude-dazzling-gates/dazzling-gates-b32c16-scope`
- commits:
    - b8a7e58 docs: both questions are answered -- record the rulings and stop calling them open
    - bf9b490 docs: correct a published symptom that was wrong, and scope a claim that was too strong

## Refs NOT selected and NOT touched

- `fix/compose-env-precedence-and-the-last-octet-mask` = `c0b3b99041504eeb4496a59daf288bf7529f7a61`
- `land/step5-and-scrub` = `41a96fb0a166d432fe11c3ab6325468d2fff9310`
- `main` = `2b1722bc884ddc3d7da0dd5b8b880155c52a1f0f`
- `scrub/f1-private-literal-gate` = `0b18f4f1c594fe27c3ee4796115d676d52f6e979`
- `step5/dockerignore-scope-and-coupling-gate` = `dceef20104dbfe3d816345a054570eba6a83802e`

