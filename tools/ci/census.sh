# tools/ci/census.sh -- the discovery library for RAMEAU's CI gates.
#
# WHY THIS FILE EXISTS
#
# On 2026-11-03 a sweep of this repository's CI reported "twelve matchers". The real number
# was 21. The list had been hand-typed beside the instruments it described, and it had drifted
# from them -- so every per-matcher claim made from it was true of twelve and silent about
# nine. That is the same defect as the one the sweep itself found in the two
# Dockerfile.dockerignore files, where `.env` denied the context root and said nothing about
# `backends/.env`: a pattern believed to cover a set that covered a prefix of it.
#
# The rule this file enforces is therefore:
#
#     A COUNT ABOUT A SET YOU OWN MUST BE DERIVED FROM THE SET, NEVER WRITTEN BESIDE IT.
#
# Every function here derives a set from the source and emits it one item per line, sorted.
# Nothing here carries a total. The caller prints the length of a list it was handed.
#
# SOURCED, NOT EXECUTED, so it carries no shebang -- which is also why the step-4 preflight's
# A5 obligation does not require it to be 100755. A library that is never invoked directly
# does not need the executable bit, and giving it one would make A5's scan report a script
# that nothing runs.
#
# EVERY FUNCTION HERE IS ITSELF A MATCHER, and that is the point rather than an irony. Each
# one fails by returning an EMPTY SET, which is indistinguishable from "there is nothing to
# find" -- exactly the failure mode three rounds of this work have been chasing. So each is
# declared between `# MATCHER:` and `# /MATCHER` markers, each has an arm in tools/ci/arms.sh
# that breaks it and requires the break to be caught, and this file is scanned by the same
# census it provides.

# ---------------------------------------------------------------------------------------
# THE SPAN MARKERS
# ---------------------------------------------------------------------------------------
# A matcher is declared by a `# MATCHER: <id>` line and closed by a `# /MATCHER` line. The
# lines between them are its span. The census below requires every pattern-decision construct
# to fall inside some span, so a new matcher written without markers FAILS rather than
# joining the population silently.
#
# An id is letters, digits and hyphens, and the TAG is case-sensitive while the id is not:
# the matcher ids are lower-case by convention and the step-4 obligation ids are A1..B7, and
# both are referenced by an arm as `# ARM: <id>` / `# OBLIGATION-ARM: <id>`.
# Mismatched, nested or unclosed markers are a failure in themselves: that is the hole a
# "remember to close it" convention would otherwise leave.

# MATCHER: marker-spans
# Emits "<start> <end> <id>" per declared span, and nothing at all when the file has none.
# awk rather than a two-pass grep because the pairing is the assertion: an unclosed or nested
# marker has to be reported as such, not silently absorbed.
marker_spans() {
  awk '
    /^[[:space:]]*#[[:space:]]*MATCHER:[[:space:]]*[a-z0-9][a-z0-9-]*[[:space:]]*$/ {
      if (open) { printf "ERROR nested-marker %d %s\n", NR, id; bad=1 }
      id = $0; sub(/^.*MATCHER:[[:space:]]*/, "", id); sub(/[[:space:]]*$/, "", id)
      open = 1; start = NR; next
    }
    /^[[:space:]]*#[[:space:]]*\/MATCHER[[:space:]]*$/ {
      if (!open) { printf "ERROR unopened-close %d -\n", NR; bad=1; next }
      printf "%d %d %s\n", start, NR, id
      open = 0; next
    }
    END { if (open) { printf "ERROR unclosed %d %s\n", start, id } }
  ' "$1"
}
# /MATCHER

# MATCHER: declared-ids
# The registry itself, and THE MOST LOAD-BEARING MATCHER IN THIS FILE: if this stops
# matching, the id set reads empty, every correspondence below passes vacuously, and the job
# reports a clean bill of health for a population it never enumerated. Its arm plants a
# declaration it must find and a lookalike it must not.
declared_ids() {
  di_tag="$1"; shift
  # grep's three statuses again, and this one was found by its own arm: with them collapsed,
  # a registry carrying NO entries returned grep's 1, pipefail propagated it, and set -e
  # killed the census step with no output at all. An empty registry has to be reportable --
  # it is the condition that would make every correspondence below pass vacuously.
  di_st=0
  di_out=$(grep -hoE "#[[:space:]]*${di_tag}:[[:space:]]*[A-Za-z0-9][A-Za-z0-9-]*" "$@") || di_st=$?
  [ "$di_st" -le 1 ] || { printf 'ERROR declared_ids could not read its inputs (grep exited %d)\n' "$di_st"; return 2; }
  [ "$di_st" -eq 0 ] || return 0
  printf '%s\n' "$di_out" | sed -E "s/^.*${di_tag}:[[:space:]]*//" | sort -u
}
# /MATCHER

# ---------------------------------------------------------------------------------------
# THE CONSTRUCT CENSUS
# ---------------------------------------------------------------------------------------
# What makes the registry non-optional. Without this, `# MATCHER:` is a convention anyone can
# decline to follow, and a pattern added without a declaration would be invisible to the
# correspondence check -- which is precisely how the count came to be twelve.
#
# SCOPE, STATED PLAINLY. This covers SHELL pattern constructs: grep, rg, git ls-files,
# git grep, ANY `find` (not only one carrying -name: `find -type f -print -quit` decides
# whether a directory is empty, and leaving it out was the first thing this census missed when
# it was tested against the file it scans), a jq program that selects, and a `case` dispatch.
#
# The rule is ONE-DIRECTIONAL: every construct must sit inside a span, but a span need not
# contain a construct. Two declared matchers -- the violation counter and its printer -- are
# built from `tr` and `awk` and carry no construct at all, and requiring the converse would
# reject them. What the one-directional rule buys is the thing that matters: a pattern added
# anywhere in a run block without a declaration FAILS, so the registry cannot be bypassed.
# It does NOT and cannot cover a matcher that is not shell source -- gitleaks' own ruleset and
# the two .dockerignore pattern sets are matchers written in other languages, and they are
# discovered by `scanner_sites` and `ignore_pattern_files` below instead. Three families, not
# one, because one would have left two of them undiscoverable and called the job complete.
#
# A line whose construct sits inside an `echo`, `printf` or `say` is prose about a matcher
# rather than a matcher, and is skipped. That exclusion is narrow on purpose: it keys on the
# line's first word, so a construct in a command substitution inside an echo is still counted.
#
# THIS CENSUS OVER-REPORTS, AND THAT IS THE DIRECTION IT MUST ERR IN. Prose that merely names
# a construct -- a failure message saying "the two grep lines" -- is reported as a hit. The
# fix for such a false positive is to reword the prose or declare a span, NEVER to narrow the
# regex, and that is not a preference: a quoted-string heuristic was written for exactly this
# and MEASURED against the three scanned files before being adopted. It would have dropped
# the two prose lines and FIVE REAL MATCHERS with them -- the probe's transferred listing,
# has_no_files, obligation_ids' own grep, and both containment helpers -- because each has a
# quoted string earlier on its line. A rule that stops seeing five live instruments to
# silence two comments is the defect this file exists to catch, so it was rejected on the
# measurement rather than on the idea.

# MATCHER: exempt-line
# Where a file says to stop censusing, and nothing below it is scanned for constructs. ONE
# file needs this: tools/ci/arms.sh, whose arm definitions QUOTE patterns as mutation
# arguments. A census over them reports the patterns each arm rewrites, not patterns the arms
# decide with, and sixteen such lines would have to carry an exemption each.
#
# The marker's failure modes are not symmetric, which is why it is armed. Failing to match
# UN-exempts the file: noisy, and safe. Matching too early silently truncates the census,
# which is the unsafe direction and the one its arm covers. The census job prints the
# truncation point for every file that carries one, so an exemption is never silent.
exempt_line() {
  awk '/^[[:space:]]*#[[:space:]]*CENSUS-EXEMPT-BELOW/ { print NR; exit }' "$1"
}
# /MATCHER

# MATCHER: construct-hits
# Emits "<file>:<line>:<text>" for every shell pattern-decision construct, stopping at the
# exemption marker if the file carries one.
construct_hits() {
  for ch_f in "$@"; do
    # grep HAS THREE STATUSES and a producer must honour all three. 0 is matches, 1 is NONE
    # -- a legitimate, common answer -- and 2 or more is "could not look". Measured while
    # building this file: with the statuses collapsed, `scanner_sites` over a file carrying
    # no scanner invocation returned 1, `pipefail` propagated it, and `set -e` killed the
    # whole census step with NO MESSAGE AT ALL. Collapsing 2 into "none" instead is finding E
    # of this repository's own gate audit, repeated inside the instrument built to prevent it.
    # A TARGET THAT CANNOT BE READ IS NOT A FILE WITH NOTHING IN IT. Measured: with
    # a directory as the target, awk fails, grep sees empty input, returns 1, and the
    # whole thing reads as "no matches" -- a clean census produced by a failure to
    # look, which is the exact shape of finding E. So assert the target first.
    [ -f "$ch_f" ] && [ -r "$ch_f" ] || { printf 'ERROR construct_hits cannot read %s\n' "$ch_f"; return 2; }
    ch_st=0
    ch_out=$(awk '/^[[:space:]]*#[[:space:]]*CENSUS-EXEMPT-BELOW/ { exit } { print }' "$ch_f" \
      | grep -nE '(^|[^[:alnum:]_])(grep|rg)([[:space:]]|$)|git[[:space:]]+(ls-files|grep)([[:space:]]|\)|$)|(^|[^[:alnum:]_])find([[:space:]]|$)|(select|startswith|endswith|test|match)\(|^[[:space:]]*case[[:space:]]') || ch_st=$?
    [ "$ch_st" -le 1 ] || { printf 'ERROR construct_hits could not read %s (grep exited %d)\n' "$ch_f" "$ch_st"; return 2; }
    [ "$ch_st" -eq 0 ] || continue
    printf '%s\n' "$ch_out" \
      | grep -vE '^[0-9]+:[[:space:]]*#' \
      | grep -vE '^[0-9]+:[[:space:]]*(echo|printf|say)[[:space:]]' \
      | sed "s|^|${ch_f}:|" || true
  done
}
# /MATCHER

# MATCHER: ignore-pattern-files
# The second census family. Each Dockerfile.dockerignore is a pattern set that decides what
# reaches a published image, and the 97727b8 defect was one of them read by nothing at all. A
# third service's ignore file arriving without an arm is the bypass this closes.
ignore_pattern_files() {
  find deploy -name 'Dockerfile.dockerignore' -o -name '.dockerignore' 2>/dev/null | sort
}
# /MATCHER

# MATCHER: scanner-sites
# The third family: an external scanner whose ruleset is the matcher. Emits one line per
# invocation site, in the same "<file>:<line>:<text>" shape as construct_hits so that one
# coverage composition serves both, so that a second scanner -- or a second invocation of
# this one -- cannot be added without an arm naming it.
scanner_sites() {
  for ss_f in "$@"; do
    # The same three-status discipline as construct_hits, and this is the function that
    # taught it: most files carry no scanner invocation at all.
    # A TARGET THAT CANNOT BE READ IS NOT A FILE WITH NOTHING IN IT. Measured: with
    # a directory as the target, awk fails, grep sees empty input, returns 1, and the
    # whole thing reads as "no matches" -- a clean census produced by a failure to
    # look, which is the exact shape of finding E. So assert the target first.
    [ -f "$ss_f" ] && [ -r "$ss_f" ] || { printf 'ERROR scanner_sites cannot read %s\n' "$ss_f"; return 2; }
    ss_st=0
    ss_out=$(grep -nE '(gitleaks|trufflehog|detect-secrets)[[:space:]]+(detect|scan|filesystem|git)' "$ss_f") || ss_st=$?
    [ "$ss_st" -le 1 ] || { printf 'ERROR scanner_sites could not read %s (grep exited %d)\n' "$ss_f" "$ss_st"; return 2; }
    [ "$ss_st" -eq 0 ] || continue
    printf '%s\n' "$ss_out" \
      | grep -vE '^[0-9]+:[[:space:]]*#' \
      | grep -vE '^[0-9]+:[[:space:]]*(echo|printf|say)[[:space:]]' \
      | sed "s|^|${ss_f}:|" || true
  done
}
# /MATCHER

# ---------------------------------------------------------------------------------------
# COVERAGE: THE COMPOSITION THAT MAKES THE REGISTRY NON-OPTIONAL
# ---------------------------------------------------------------------------------------
# NOT a matcher, and deliberately not declared as one: it carries no pattern of its own. It
# joins a producer's "<file>:<line>:<text>" output against that file's declared spans and
# emits the hits that fall outside every one of them. Each emitted line is a pattern that
# decides something and has no arm, which is the condition this whole round exists to fail.
#
# When a file has NO spans at all, every hit is uncovered -- which is the correct reading and
# the opposite of the convenient one. A missing registry must not look like a clean file.
uncovered() {
  uc_fn="$1"; shift
  for uc_f in "$@"; do
    uc_spans=$(marker_spans "$uc_f" | awk '$1 != "ERROR" {printf "%s-%s;", $1, $2}')
    "$uc_fn" "$uc_f" | awk -v spans="$uc_spans" -F: '
      BEGIN {
        m = 0
        n = split(spans, S, ";")
        for (i = 1; i <= n; i++) if (S[i] != "") { split(S[i], R, "-"); m++; lo[m] = R[1] + 0; hi[m] = R[2] + 0 }
      }
      {
        ln = $2 + 0
        covered = 0
        for (i = 1; i <= m; i++) if (ln >= lo[i] && ln <= hi[i]) covered = 1
        if (!covered) print $0
      }'
  done
}

# ---------------------------------------------------------------------------------------
# THE POPULATION OF JOBS AND FILES
# ---------------------------------------------------------------------------------------

# MATCHER: workflow-files
# Discovered, not named. A census that hard-coded `ci.yml` would go quiet the day a second
# workflow landed -- and the quiet would look exactly like a clean result.
workflow_files() {
  find .github/workflows -maxdepth 1 -type f \( -name '*.yml' -o -name '*.yaml' \) 2>/dev/null | sort
}
# /MATCHER

# MATCHER: scanned-files
# The set of files the census reads for matchers and arms, DISCOVERED rather than listed.
# The first version of the census job carried `$WFS tools/ci/census.sh tools/ci/arms.sh` --
# a hand-typed list of the files whose hand-typed lists it existed to replace. A third
# script added under tools/ci/ would have been scanned by nothing.
scanned_files() {
  {
    workflow_files
    find tools/ci -maxdepth 1 -type f -name '*.sh' 2>/dev/null
  } | sort -u
}
# /MATCHER

# MATCHER: job-names
# Discovered from the parsed YAML rather than from a list, for the same reason. A workflow
# with no `jobs:` key yields an empty set, so the caller must treat empty as a failure; this
# function does not decide that, it only reports what it found.
job_names() {
  python3 - "$1" <<'JOBNAMES'
import sys, yaml
with open(sys.argv[1]) as fh:
    doc = yaml.safe_load(fh) or {}
for name in sorted((doc.get("jobs") or {}).keys()):
    print(name)
JOBNAMES
}
# /MATCHER

# MATCHER: obligation-ids
# The step-4 obligations, derived from the messages the preflight itself emits rather than
# from the table in RELEASE-SCOPE.md. Three shapes carry an id: a violation registered with
# `v "<ID> ..."`, a not-yet-due note registered with `d "<ID> ..."`, and the pipe-joined
# deferred string the `src` step writes. Deriving from all three is what makes the set the
# preflight's own rather than a second list that can drift from it.
obligation_ids() {
  # Each half is collected separately and may legitimately be empty -- the same three-status
  # discipline as declared_ids, for the same measured reason.
  oi_a=""
  oi_b=""
  oi_st=0
  oi_raw=$(grep -hE '[[:space:]][vd][[:space:]]+"[AB][0-9]+[[:space:]]' "$@") || oi_st=$?
  [ "$oi_st" -le 1 ] || { printf 'ERROR obligation_ids could not read its inputs (grep exited %d)\n' "$oi_st"; return 2; }
  if [ "$oi_st" -eq 0 ]; then
    oi_a=$(printf '%s\n' "$oi_raw" | grep -vE '^[[:space:]]*(echo|printf|say)[[:space:]]' | grep -oE '[AB][0-9]+' || true)
  fi
  oi_st=0
  oi_raw=$(grep -hE 'deferred="[^"]*"' "$@") || oi_st=$?
  [ "$oi_st" -le 1 ] || { printf 'ERROR obligation_ids could not read its inputs (grep exited %d)\n' "$oi_st"; return 2; }
  if [ "$oi_st" -eq 0 ]; then
    oi_b=$(printf '%s\n' "$oi_raw" \
      | grep -vE '^[[:space:]]*(echo|printf|say)[[:space:]]' \
      | grep -oE 'deferred="[^"]*"' \
      | tr '|' '\n' | grep -oE '(^|")[AB][0-9]+[[:space:]]' | grep -oE '[AB][0-9]+' || true)
  fi
  printf '%s\n%s\n' "$oi_a" "$oi_b" | awk 'NF' | sort -u
}
# /MATCHER

# MATCHER: says
# A containment test over a function's output, used by the controls that exercise everything
# above. It is declared and armed like the rest for the obvious reason: were `says` to return
# true unconditionally, every control in the census job would pass and the job would certify
# a library it had not exercised. -F so the needle is a literal.
says() { printf '%s' "$1" | grep -qF -- "$2"; }
# /MATCHER
