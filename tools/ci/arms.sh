#!/usr/bin/env bash
# tools/ci/arms.sh -- the failing arm for every gate in this repository.
#
# Run it:   bash tools/ci/arms.sh            all arms
#           bash tools/ci/arms.sh uvicorn    only arms whose label matches (literal substring)
#
# WHY THIS IS COMMITTED
#
# Through three rounds the arms lived in a scratch directory and died with the session that
# wrote them. That made every claim about them unverifiable by anyone else, and it meant the
# seven obligations that need an assembled source tree were "deferred" in the weakest sense:
# the work of writing their arms was deferred too. Committed, they are a command step 4 can
# run rather than a round of work it has to repeat.
#
# WHAT AN ARM IS
#
# A mutation that a check must catch, plus the message the check must produce. An arm that
# exits nonzero for the WRONG REASON is not a pass, and this harness distinguishes the two:
# FAIL-TO-FIRE (the mutation was not caught at all) and WRONG-REASON (something failed, but
# not the thing under test) are different bugs and are reported differently.
#
# A MUTATION THAT ITSELF FAILS IS NOT A PASS. If the anchor does not match, or the planted
# file is refused by .gitignore, the arm reports MUTATION-FAILED and nothing is claimed. That
# distinction caught a real false pass: `git add model.gguf` is refused by this repository's
# own .gitignore, so the A3 arm had been testing nothing while reading green.
#
# EVERY ARM RUNS IN A FRESH CLONE, never in the working tree. An earlier harness reset the
# real tree between arms, which both leaked state and pointed a destructive operation at the
# checkout. A clone per arm is isolated by construction.

set -uo pipefail   # NOT -e: this harness has to survive an arm failing in order to report it

SRC="${ARMS_SRC:-$(git rev-parse --show-toplevel)}"
WORK="$(mktemp -d)"
AR="$WORK/clone"
STEPS="$WORK/steps"
FILTER="${1:-}"
PASS=0
FAIL=0
SKIP=0
RESULTS=""
trap 'rm -rf "$WORK"' EXIT

note()   { printf '%s\n' "$*" >&2; }
record() { RESULTS="$RESULTS$1"$'\n'; }

# ---------------------------------------------------------------------------------------
# DEPENDENCIES, ASSERTED AND NAMED, BEFORE ANY ARM RUNS
# ---------------------------------------------------------------------------------------
# FAILS CLOSED, for the same reason tools/git-hooks/pre-push fails closed when gitleaks is
# absent: a harness that quietly skips the arms whose tool is missing manufactures the
# appearance of coverage.
#
# Not hypothetical. The first CI run of this file scored the gitleaks arm as WRONG-REASON,
# because the `arms` job had not installed gitleaks and the canary step it drives exited 127.
# Everything downstream behaved correctly -- the canary's control reported "exited 127 WITHOUT
# writing a report, which means it crashed rather than found the canary", and the harness
# reported WRONG-REASON rather than scoring it -- but one arm reading WRONG-REASON is a far
# worse diagnosis than the whole suite refusing and naming the binary. Each arm runs a real CI
# step against a mutated clone, so the arms need what those steps need.
arms_missing=""
for arms_dep in git python3 docker gitleaks; do
  command -v "$arms_dep" >/dev/null 2>&1 || arms_missing="$arms_missing $arms_dep"
done
python3 -c 'import yaml' >/dev/null 2>&1 || arms_missing="$arms_missing python3-yaml"
docker info >/dev/null 2>&1 || arms_missing="$arms_missing docker-daemon"
if [ -n "$arms_missing" ]; then
  note "ARMS REFUSED: needed and not available:$arms_missing"
  note ""
  note "Every arm runs one real CI step against a mutated clone, so the arms need what those"
  note "steps need. Skipping the arms whose tool is missing would report coverage this run"
  note "does not have, so this refuses instead and names what is absent."
  exit 2
fi

# MATCHER: arm-expectation-match
# THE HARNESS'S OWN MATCHER, and the most load-bearing one in this file: were it to match
# unconditionally, every arm below would score ARMED whatever actually happened, and this
# file would be the most convincing inert instrument in the repository. -F, so the
# expectation is a literal string and never a regex that quietly matches more than intended.
output_says() { printf '%s' "$1" | grep -qF -- "$2"; }
# /MATCHER

# ---------------------------------------------------------------------------------------
# the clone, the mutation, the runners
# ---------------------------------------------------------------------------------------

# A clone of HEAD with the working tree's tracked modifications overlaid and committed, so
# an arm tests the code in front of you rather than the code you last pushed.
fresh() {
  rm -rf "$AR" "$STEPS"
  git clone -q --local "$SRC" "$AR" || return 1
  ( cd "$SRC" && git diff --name-only HEAD ) | while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    mkdir -p "$AR/$(dirname "$rel")"
    cp "$SRC/$rel" "$AR/$rel"
  done
  ( cd "$AR" && git add -A && git -c user.email=arms@example.invalid -c user.name=arms \
      commit -qm "arms: working tree under test" ) >/dev/null 2>&1
  ( cd "$AR" && python3 tools/ci/extract-steps.py .github/workflows/ci.yml "$STEPS" ) >/dev/null
}

# Exact, single-occurrence replacement. An anchor that matches zero times or more than once
# is a MUTATION-FAILED, never a silent no-op: a mutation whose anchor did not match is not
# a pass, and reading green from one is how an inert guard survives.
mutate() {
  python3 - "$AR/$1" "$2" "$3" <<'MUTATE'
import io, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = io.open(path, encoding="utf-8").read()
n = s.count(old)
if n != 1:
    sys.stderr.write("mutate: anchor occurs %d times in %s (want exactly 1)\n" % (n, path))
    sys.exit(2)
io.open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
MUTATE
  mu_rc=$?
  [ "$mu_rc" -eq 0 ] || return "$mu_rc"
  # RE-EXTRACT AUTOMATICALLY when a workflow was mutated. Leaving this to each caller was a
  # per-arm chance to forget, and forgetting it runs the UNMUTATED step -- which exits 0 and
  # scores FAIL-TO-FIRE, blaming the check for the harness's omission.
  if output_says "$1" ".github/workflows/"; then reextract; fi
}

# THE SAME MUTATION, WITH THE ANCHOR BASE64-ENCODED. Five arms need an anchor carrying
# quotes, backslashes or both, and threading those through the shell quoting of an arm
# definition produced four MUTATION-FAILED results before this existed. Base64 removes every
# quoting layer between the arm and the file; what each anchor decodes to is in a comment
# above its use. The harness reported those four rather than scoring them, which is the
# distinction that makes it worth trusting at all.
mutate_b64() {
  python3 - "$AR/$1" "$2" "$3" <<'MUTATE64'
import base64, io, sys
path = sys.argv[1]
old = base64.b64decode(sys.argv[2]).decode()
new = base64.b64decode(sys.argv[3]).decode()
s = io.open(path, encoding="utf-8").read()
n = s.count(old)
if n != 1:
    sys.stderr.write("mutate_b64: anchor occurs %d times in %s (want exactly 1)\n" % (n, path))
    sys.exit(2)
io.open(path, "w", encoding="utf-8").write(s.replace(old, new, 1))
MUTATE64
  mu_rc=$?
  [ "$mu_rc" -eq 0 ] || return "$mu_rc"
  if output_says "$1" ".github/workflows/"; then reextract; fi
}

# Re-extract after a mutation to ci.yml, so the step that runs is the mutated one.
#
# THE DIRECTORY IS CLEARED FIRST, and that is a fix rather than tidiness. Extracting over a
# previous extraction leaves the OLD step files in place, so a mutation that inserts or
# renames a step leaves two files carrying one index. `step_path` globs, and printf '%s'
# over two matches concatenates them WITHOUT a separator, so the result reads like a single
# path and the basename taken from it is the wrong step's. Found by the step-map arm below,
# which scored FAIL-TO-FIRE on a mutation it had plainly applied: the stale
# build__08__licence-and-scope.sh was still there beside the newly inserted one, so the
# expectation matched and the check said ok. Every arm that renames or reorders a step was
# exposed to this.
reextract() {
  rm -rf "$STEPS"
  mkdir -p "$STEPS"
  ( cd "$AR" && python3 tools/ci/extract-steps.py .github/workflows/ci.yml "$STEPS" ) >/dev/null
}

step_path() { printf '%s' "$STEPS"/"$1"__*.sh; }

run_step() {
  ( cd "$AR" \
    && GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/sum" GITHUB_WORKSPACE="$AR" \
       SRC_PRESENT="${SRC_PRESENT:-false}" \
       bash "$(step_path "$1")" 2>&1 )
}

# The whole preflight, with each step's outputs wired into the next from the step's own
# output file rather than from an assumption about what it wrote. The state step reads its
# inputs through `env:` expressions the extractor cannot evaluate, so they are supplied here.
run_preflight() {
  : > "$WORK/out"
  : > "$WORK/sum"
  pf_all=""
  pf_rc=0
  for pf_s in step4__01 step4__02 step4__03 step4__04; do
    pf_present=$(awk -F= '$1 == "present" {print $2}' "$WORK/out" | tail -1)
    pf_out=$( cd "$AR" && GITHUB_OUTPUT="$WORK/out" GITHUB_STEP_SUMMARY="$WORK/sum" \
                GITHUB_WORKSPACE="$AR" SRC_PRESENT="${pf_present:-false}" \
                bash "$(step_path "$pf_s")" 2>&1 ) || pf_rc=$?
    pf_all="$pf_all$pf_out"$'\n'
    if [ "$pf_rc" -ne 0 ]; then printf '%s' "$pf_all"; return "$pf_rc"; fi
  done
  pf_get() { awk -F= -v k="$1" '$1 == k {sub("^" k "=", ""); print}' "$WORK/out" | tail -1; }
  pf_out=$( cd "$AR" \
    && PRESENT="$(pf_get present)" REPO_CHECKED="$(pf_get repo_checked)" \
       REPO_V="$(pf_get repo_violations)" REPO_D="$(pf_get repo_deferred)" \
       SRC_CHECKED="$(pf_get src_checked)" SRC_V="$(pf_get src_violations)" \
       SRC_D="$(pf_get src_deferred)" \
       GITHUB_STEP_SUMMARY="$WORK/sum" GITHUB_OUTPUT="$WORK/out2" \
       bash "$(step_path step4__05)" 2>&1 ) || pf_rc=$?
  printf '%s%s' "$pf_all" "$pf_out"
  return "$pf_rc"
}

# ---------------------------------------------------------------------------------------
# the three verdicts
# ---------------------------------------------------------------------------------------
# $1 label  $2 expected literal  $3 setup (eval'd in the clone)  $4 runner  $5 want-rc
judge() {
  j_label="$1"; j_expect="$2"; j_setup="$3"; j_runner="$4"; j_want="$5"
  if [ -n "$FILTER" ] && ! output_says "$j_label" "$FILTER"; then
    SKIP=$((SKIP + 1))
    return 0
  fi
  if ! fresh; then
    record "HARNESS-FAILED  | $j_label | the clone could not be prepared"
    FAIL=$((FAIL + 1)); return 0
  fi
  j_mrc=0
  ( cd "$AR" && eval "$j_setup" ) > "$WORK/mut" 2>&1 || j_mrc=$?
  if [ "$j_mrc" -ne 0 ]; then
    record "MUTATION-FAILED | $j_label | the mutation exited $j_mrc; NOTHING was tested"
    note "--- mutation output for: $j_label"; note "$(cat "$WORK/mut")"
    FAIL=$((FAIL + 1)); return 0
  fi
  j_rc=0
  j_out=$($j_runner) || j_rc=$?
  if [ "$j_want" = "fail" ] && [ "$j_rc" -eq 0 ]; then
    record "FAIL-TO-FIRE    | $j_label | exited 0 -- the mutation was NOT caught"
    note "--- tail for: $j_label"; note "$(printf '%s' "$j_out" | tail -8)"
    FAIL=$((FAIL + 1)); return 0
  fi
  if [ "$j_want" = "pass" ] && [ "$j_rc" -ne 0 ]; then
    record "UNEXPECTED-FAIL | $j_label | exited $j_rc, expected 0"
    note "--- tail for: $j_label"; note "$(printf '%s' "$j_out" | tail -12)"
    FAIL=$((FAIL + 1)); return 0
  fi
  if ! output_says "$j_out" "$j_expect"; then
    record "WRONG-REASON    | $j_label | exit $j_rc, but never said: $j_expect"
    note "--- tail for: $j_label"; note "$(printf '%s' "$j_out" | tail -14)"
    FAIL=$((FAIL + 1)); return 0
  fi
  if [ "$j_want" = "pass" ]; then
    record "PASSES-AGAIN    | $j_label | exit 0, and said it"
  else
    record "FIRES           | $j_label | exit $j_rc, and said why"
  fi
  PASS=$((PASS + 1))
}

fires()  { judge "$1" "$2" "$3" "${4:-run_preflight}" fail; }
passes() { judge "$1" "$2" "$3" "${4:-run_preflight}" pass; }

run_build_loopback()  { run_step build__05; }
run_build_envfile()   { run_step build__07; }
run_build_licence()   { run_step build__08; }
run_images_probe()    { run_step images__02; }
run_secrets_canary()  { run_step secrets__02; }
run_census_controls() { run_step census__01; }
run_census_main()     { run_step census__02; }

# ---------------------------------------------------------------------------------------
# THE POSITIONAL STEP MAP, ASSERTED BEFORE ANY ARM RUNS
# ---------------------------------------------------------------------------------------
# EVERY RUNNER ABOVE ADDRESSES A STEP BY ITS INDEX, AND AN INDEX IS SILENT WHEN IT MOVES.
# Measured 2026-11-03 on the merge that brought the step-5 and scrub branches together:
# grafting three gates into `build` pushed "licence and scope invariants" from build__08 to
# build__11 while `run_build_licence` went on addressing build__08 -- by then the
# dockerignore gate. The apache-licence-substring arm would have mutated the licence step
# and judged a different step's output. It would not have errored, and it would not have
# been scored as a harness fault; it would simply have reported on the wrong subject.
#
# WHY A PREFLIGHT RATHER THAN A CHECK INSIDE run_step. A guard that fires inside the runner
# returns non-zero with a message, and `judge` would compare that message against the arm's
# expectation, miss it, and record WRONG-REASON. That is a FAIL, so not a false pass -- but
# it reads as "the gate fired for the wrong reason" when the truth is that NOTHING WAS
# TESTED. This is the same judgement the dependency check at the top of this file makes:
# one arm reading WRONG-REASON is a far worse diagnosis than the whole suite refusing and
# naming the cause. So the map is asserted once, before any arm runs, and a fault here can
# never be dressed up as an arm's verdict.
#
# THE EXPECTATIONS ARE SUBSTRINGS OF THE SLUG, not whole filenames: extract-steps.py
# truncates a long step name, so pinning the full name would make this gate go red on an
# edit to a step's prose -- which is growth, not breakage.
# STEP-MAP-DATA
STEP_MAP="build__05:in-network-profile
build__07:compose-env-must-actually-reach
build__08:licence-and-scope
images__02:ignore-files-patterns
secrets__02:prove-the-scanner-fires
census__01:discovery-library
census__02:discover-the-population
step4__01:assert-what-cannot-be-absent
step4__02:obligations-that-hold
step4__03:is-the-source-assembled
step4__04:assembled-source-obligations
step4__05:state"
# /STEP-MAP-DATA

# Extracted from the WORKING TREE rather than from a clone: `fresh` commits exactly those
# modifications before extracting, so the mapping is the same one every arm will see, and
# this costs no clone.
mkdir -p "$STEPS"
if ! ( cd "$SRC" && python3 tools/ci/extract-steps.py .github/workflows/ci.yml "$STEPS" ) \
     >/dev/null 2>&1; then
  note "ARMS REFUSED: the step extractor failed, so the positional map cannot be checked"
  note "and every arm below would be addressing steps nobody has confirmed exist."
  exit 2
fi

# THE MAP IS CHECKED AGAINST THE REFERENCES DISCOVERED IN THIS FILE, not trusted to be
# complete. A hand-kept map is the same defect one level up: a runner added later without a
# map entry would escape this check in silence, which is precisely the failure being closed.
#
# The two decisions below are DECLARED MATCHERS with arms of their own, like every other
# pattern that decides something in this repository. They are the harness deciding what the
# arms are allowed to address, so they are exactly the kind of instrument the registry
# exists for -- and the census said so before this comment did.

# MATCHER: step-map-refs
# Every positional id this file ADDRESSES, discovered rather than listed.
#
# DISCOVERY IS ANCHORED ON THE ID ITSELF, not on an adjacent `run_step`/`step_path`. The
# ids are not all written the same way: run_preflight holds four of them in a shell list
# (`for pf_s in step4__01 ...`) and calls step_path with the loop variable, so a pattern
# keyed on the caller finds EIGHT OF TWELVE and reports the other four as stale entries --
# measured, on a baseline with nothing wrong with it. A guard that is red on correct
# content gets switched off, so the pattern has to find an id however it is spelt.
#
# Two exclusions keep that sound: COMMENT LINES (this file's own prose names ids by way of
# example, and an example is not a reference) and the STEP-MAP-DATA block (the map must not
# be allowed to verify itself into completeness -- every entry would be "addressed" by its
# own row). The file to read is an argument so an arm can point it at a mutated copy in the
# clone; nothing else about it is configurable.
step_map_refs() {
  awk '
    /^# \/STEP-MAP-DATA$/ { smr_skip = 0; next }
    /^# STEP-MAP-DATA$/   { smr_skip = 1; next }
    smr_skip              { next }
    /^[[:space:]]*#/      { next }
                          { print }
  ' "$1" | grep -oE '\b[a-z0-9]+__[0-9]+\b' | sort -u
}
# /MATCHER

# MATCHER: step-map-expectation
# Whether one id resolves to the step the map says it does. Prints a finding, or nothing.
#
# THE EXPECTATION IS A SUBSTRING OF THE SLUG, deliberately: extract-steps.py truncates a
# long step name, so pinning the whole filename would make this go red on an edit to a
# step's prose -- growth, not breakage. The unresolved case is kept separate from the
# wrong-slug case because they are different faults: a glob that matched nothing means the
# index is off the end, and reporting that as "the wrong slug" would describe a file that
# does not exist.
# The glob is counted rather than passed through `step_path`, because printf '%s' over two
# matches concatenates them with no separator and the result still LOOKS like one path. The
# ambiguity leg is a backstop, not a measured leg: with reextract clearing the directory
# nothing should reach it, and it is here because something did -- it is what turned a
# FAIL-TO-FIRE into the reextract fix above rather than into a weakened expectation.
step_map_expectation() {
  sme_n=0
  sme_last=""
  for sme_p in "$STEPS"/"$1"__*.sh; do
    [ -e "$sme_p" ] || continue
    sme_n=$((sme_n + 1))
    sme_last=$sme_p
  done
  if [ "$sme_n" -eq 0 ]; then
    printf '    %s -- NO SUCH STEP (expected a slug containing: %s)\n' "$1" "$2"
    return 0
  fi
  if [ "$sme_n" -gt 1 ]; then
    printf '    %s -- AMBIGUOUS: %d step files carry this index (expected: %s)\n' \
           "$1" "$sme_n" "$2"
    return 0
  fi
  sme_base=${sme_last##*/}
  case "$sme_base" in
    *"$2"*) ;;
    *) printf '    %s -> %s (expected a slug containing: %s)\n' "$1" "$sme_base" "$2" ;;
  esac
}
# /MATCHER

# The check itself, on STDOUT rather than through `note`, for one reason: an arm reads a
# runner's STDOUT to decide whether the right thing failed, and a refusal written to stderr
# would be judged WRONG-REASON however correct it was.
step_map_check() {
  smc_refs=$(step_map_refs "$1")
  smc_mapped=$(printf '%s\n' "$STEP_MAP" | awk -F: 'NF{print $1}' | sort -u)
  smc_unmapped=$(comm -23 <(printf '%s\n' "$smc_refs") <(printf '%s\n' "$smc_mapped"))
  smc_stale=$(comm -13 <(printf '%s\n' "$smc_refs") <(printf '%s\n' "$smc_mapped"))
  smc_bad=""
  smc_checked=0
  smc_oldifs=$IFS
  IFS='
'
  for smc_row in $STEP_MAP; do
    IFS=$smc_oldifs
    smc_find=$(step_map_expectation "${smc_row%%:*}" "${smc_row#*:}")
    [ -z "$smc_find" ] || smc_bad="$smc_bad$smc_find"$'\n'
    smc_checked=$((smc_checked + 1))
    IFS='
'
  done
  IFS=$smc_oldifs

  if [ -z "$smc_unmapped" ] && [ -z "$smc_stale" ] && [ -z "$smc_bad" ]; then
    printf 'step map ok: %d positional reference(s), each resolving to the step it names\n' \
           "$smc_checked"
    return 0
  fi

  printf 'ARMS REFUSED: the positional step map does not describe this workflow.\n\n'
  [ -z "$smc_unmapped" ] || {
    printf '  ADDRESSED BUT NOT IN THE MAP -- these are checked by nothing:\n'
    printf '%s\n' "$smc_unmapped" | awk 'NF{print "    " $0}'
  }
  [ -z "$smc_stale" ] || {
    printf '  IN THE MAP AND ADDRESSED BY NOTHING -- a stale entry asserting nothing:\n'
    printf '%s\n' "$smc_stale" | awk 'NF{print "    " $0}'
  }
  [ -z "$smc_bad" ] || {
    printf '  RESOLVES TO THE WRONG STEP -- an arm here would mutate one step and judge\n'
    printf '  another, and would report a verdict rather than an error:\n'
    printf '%s' "$smc_bad"
  }
  printf '\n'
  printf 'A step was inserted, removed or renamed. Fix the INDEX (or the expectation) in\n'
  printf 'STEP_MAP and in the runner that addresses it -- do not delete the entry to get\n'
  printf 'green, because the entry is the only thing standing between an index that moved\n'
  printf 'and an arm that silently reports on the wrong subject.\n'
  return 2
}

# The runner the two arms below use. It reads the CLONE's copy of this file, so an arm that
# adds a runner there is discovered the same way a real one would be.
run_step_map() { step_map_check "$AR/tools/ci/arms.sh"; }

sm_rc=0
sm_report=$(step_map_check "$SRC/tools/ci/arms.sh") || sm_rc=$?
rm -rf "$STEPS"
note "$sm_report"
[ "$sm_rc" -eq 0 ] || exit "$sm_rc"

# A correct, minimal services/ tree: every source-dependent obligation satisfied. Built in
# the clone, never committed, discarded with the clone. It is NOT the real source and nothing
# here pretends otherwise -- an arm against a stub shows the assertion CAN fire, which is the
# strongest claim available before step 4 and is not the same as having run at the transition.
#
# STEP 4 HAS NOW HAPPENED, and the line above anticipated this without being able to act on
# it. Until the source landed, `services/` did not exist and `mkdir -p` built the whole of
# what the obligations would read. Now `services/router/` holds 29 real files and
# `services/portal/` 28, so the same `mkdir -p` OVERLAYS them instead of creating them, and
# every arm that works by taking something away stopped reaching its state: `rm
# services/router/skills/registry.md` left fifteen real files in `skills/`, so B2's "EMPTY"
# was never true, the check correctly did not fire, and the arm read that as the check being
# broken. Three arms failed that way and one of them is the B3 twin.
#
# `rm -rf services` first makes the stub AUTHORITATIVE rather than additive: what the
# obligations read is exactly what the line below builds, as it was before the source
# existed, and as it will stay however the real tree grows. This runs in the disposable
# clone under $WORK -- `git clone --local` at line 92, mutations eval'd via `cd "$AR"` -- so
# it removes nothing from the repository and the checkout is never touched.
STUB='
rm -rf services
mkdir -p services/router/backends services/router/skills services/portal/src services/portal/public
printf "fastapi\nuvicorn[standard]==0.30.6\n" > services/router/requirements.txt
: > services/router/main.py
: > services/router/backends/ollama.py
: > services/router/skills/registry.md
for f in package.json package-lock.json index.html vite.config.js nginx.conf; do : > services/portal/$f; done
: > services/portal/src/App.jsx
: > services/portal/public/favicon.ico
'
with_stub() { printf '%s\n%s\n' "$STUB" "$1"; }

# CENSUS-EXEMPT-BELOW: from here down this file QUOTES patterns as mutation arguments
# rather than deciding with them. The construct census stops at this line, and the census job
# prints the truncation point so the exemption is visible rather than assumed. Everything
# above it -- the harness, where the real decisions are made -- is censused, and its one
# matcher, output_says, is declared and armed like any other.

# =======================================================================================
# THE TWELVE STEP-4 OBLIGATIONS
# =======================================================================================
# Tagged OBLIGATION-ARM so the matcher registry and the obligation registry are two
# correspondences rather than one muddled set. The census job compares these ids against the
# obligation ids it derives from the preflight's own messages.

# OBLIGATION-ARM: A1
fires "A1 the inert .dockerignore name returns" \
      "that name is NOT read for a Dockerfile outside its context" \
      ': > deploy/images/router/.dockerignore'

# OBLIGATION-ARM: A2
fires "A2 a .jar at the top level" "A2 a .jar is tracked" \
      ': > probe.jar && git add probe.jar'
fires "A2 a .jar two directories down" "nested/deep/hidden.jar" \
      'mkdir -p nested/deep && : > nested/deep/hidden.jar && git add nested/deep/hidden.jar'

# OBLIGATION-ARM: A3
# -f because .gitignore is the FIRST barrier and refuses the add outright. Without it the
# mutation silently does nothing, which is how this arm once read green while testing air.
fires "A3 model weights tracked" "A3 model weights are tracked" \
      ': > model.gguf && git add -f model.gguf'
fires "A3 a .safetensors two directories down" "sub/deep/w.safetensors" \
      'mkdir -p sub/deep && : > sub/deep/w.safetensors && git add -f sub/deep/w.safetensors'

# OBLIGATION-ARM: A4
# NOTICE NOW EXISTS, so adding a font no longer reaches the state this arm names. It landed
# with the nine woff2 faces at step 4, and until then its absence was the default the arm
# could rely on without saying so. Planting a font beside an existing NOTICE is the case A4
# is supposed to ACCEPT -- the arm below it checks exactly that -- so this one stopped being
# a mutation at all and reported the check as broken. Removing NOTICE is now part of the
# mutation rather than part of the background.
fires "A4 a font with no NOTICE" "font file(s) are tracked but NOTICE is absent" \
      'git rm -q --cached NOTICE && rm -f NOTICE && : > Inter.woff2 && git add Inter.woff2'
passes "A4 a font WITH NOTICE, and A4 moves from not-yet-due to checked" \
       "repository-only obligations:  5 checked, 0 violated, 0 not yet due" \
       ': > Inter.woff2 && printf "RAMEAU\nOFL fonts.\n" > NOTICE && git add Inter.woff2 NOTICE'

# OBLIGATION-ARM: A5
fires "A5 a shebang file recorded 100644 in the index" \
      "recorded 100644 in the index and will NOT be executed" \
      'git update-index --chmod=-x tools/git-hooks/pre-push'
fires "A5 a SECOND shebang file 100644 -- the case the old step passed green" \
      "second-probe.sh" \
      'printf "#!/usr/bin/env bash\necho x\n" > second-probe.sh && git add second-probe.sh'

passes "the correct stub -- ASSEMBLED AND CLEAN" "STATE: ASSEMBLED AND CLEAN" "$STUB"

# OBLIGATION-ARM: B1
fires "B1 requirements.txt missing" "the router build stops on COPY requirements.txt" \
      "$(with_stub 'rm services/router/requirements.txt')"
fires "B1 no top-level .py" "copies nothing" \
      "$(with_stub 'rm services/router/main.py')"
fires "B1 main.py gone but another top-level .py present" \
      "main.py is missing, and the Dockerfile's CMD is uvicorn main:app" \
      "$(with_stub 'mv services/router/main.py services/router/app.py')"

# OBLIGATION-ARM: B2
fires "B2 skills/ empty" "skills/ exists but is EMPTY" \
      "$(with_stub 'rm services/router/skills/registry.md')"
fires "B2 a .py appears in skills/" "it was measured as Markdown-only" \
      "$(with_stub ': > services/router/skills/loader.py')"

# OBLIGATION-ARM: B3
fires "B3 backends/ empty" "backends/ exists but is EMPTY" \
      "$(with_stub 'rm services/router/backends/ollama.py')"

# OBLIGATION-ARM: B4
fires "B4 a module in a directory no COPY names" \
      "dropped TWICE and fail at import after a green build" \
      "$(with_stub 'mkdir -p services/router/strategies && : > services/router/strategies/tiers.py')"
fires "B4 a module buried three deep" "services/router/deep/deeper/buried.py" \
      "$(with_stub 'mkdir -p services/router/deep/deeper && : > services/router/deep/deeper/buried.py')"
passes "B4 near-miss: a test file one directory down is NOT a stray" \
       "STATE: ASSEMBLED AND CLEAN" \
       "$(with_stub 'mkdir -p services/router/pkg && : > services/router/pkg/test_helper.py')"

# OBLIGATION-ARM: B5
fires "B5 bare uvicorn, pinned" "pins bare" \
      "$(with_stub "printf 'fastapi\nuvicorn==0.30.6\n' > services/router/requirements.txt")"
fires "B5 bare uvicorn with an environment marker" "pins bare" \
      "$(with_stub "printf 'fastapi\nuvicorn ; python_version>=\"3.11\"\n' > services/router/requirements.txt")"
fires "B5 no uvicorn line at all" "names no uvicorn" \
      "$(with_stub "printf 'fastapi\n' > services/router/requirements.txt")"

# OBLIGATION-ARM: B6
fires "B6 nginx.conf missing" "portal/nginx.conf is missing" \
      "$(with_stub 'rm services/portal/nginx.conf')"
fires "B6 public/ missing" "portal/public/ is missing" \
      "$(with_stub 'rm -rf services/portal/public')"

# OBLIGATION-ARM: B7
fires "B7 a .env inside a build context" "credential-shaped file(s) inside a build context" \
      "$(with_stub ': > services/router/.env')"
fires "B7 a .pem inside a re-admitted subtree -- where the ignore file was blind" \
      "services/router/backends/leak.pem" \
      "$(with_stub ': > services/router/backends/leak.pem')"

fires "half-assembled: one context present, one absent" "B6" \
      "$(with_stub 'rm -rf services/portal')"

# =======================================================================================
# THE FOUR STATE NAMES, AND THE FIFTH THAT IS NOT A STATE
# =======================================================================================
run_state_bare() {
  ( cd "$AR" && PRESENT=false REPO_CHECKED= REPO_V= REPO_D= SRC_CHECKED= SRC_V= SRC_D= \
      GITHUB_STEP_SUMMARY="$WORK/sum" GITHUB_OUTPUT="$WORK/out2" \
      bash "$(step_path step4__05)" 2>&1 )
}
# "No source present" was the repository's own condition until step 4 and is now a state it
# has permanently left, so the arm has to CONSTRUCT it instead of inheriting it. Without the
# removal the mutation lands on an assembled tree and the preflight says ASSEMBLED AND
# VIOLATED -- the right answer to a different question, and indistinguishable in a substring
# test from the check being broken.
fires "state: a repository violation with no source present" \
      "STATE: NOT ASSEMBLED YET, AND VIOLATED" \
      'rm -rf services && : > probe.jar && git add probe.jar'
fires "state: an assembled source with a violation" "STATE: ASSEMBLED AND VIOLATED" \
      "$(with_stub 'mkdir -p services/router/strategies && : > services/router/strategies/tiers.py')"
fires "state: INDETERMINATE when the repository step did not finish" \
      "STATE: INDETERMINATE" 'true' run_state_bare

# =======================================================================================
# ONE ARM PER MATCHER
# =======================================================================================
# Each breaks the matcher and requires ITS OWN CONTROL to catch the break. A control that
# cannot fail is the defect this repository has chased through three rounds, so the controls
# get arms exactly as the obligations do.

# ARM: gitleaks-ruleset
# RE-ANCHORED 2026-11-03, because the step this arm mutates moved out of the workflow.
# The canary used to be planted inline in secrets__02; it is now planted by
# tools/prove_scanner_fires.py, which carries both halves of the control -- the eleven
# positive shapes and the three near-misses -- in one scan. The old anchor was
# `printf 'B%.0s' $(seq 16))"` in ci.yml, and after the move it matched ZERO times.
# `mutate` would have exited 2 and the harness would have scored MUTATION-FAILED:
# loud rather than silent, but still an arm testing nothing. A mutation whose anchor
# did not match is NOT-PLANTED, never a pass.
#
# Same mutation, same meaning: the zero-entropy near-miss is handed a real random key
# body, so it stops being a near-miss and the specificity half must notice.
# No `&& reextract` -- the mutated file is not a workflow, so the extracted step script
# is unchanged and re-extracting would be a no-op that reads as though it mattered.
#
# PROVED BEFORE BEING COMMITTED, on gitleaks 8.30.1: the mutated near-miss fired in 2 of
# 13 rounds and tier 1b went red naming it and the rule (aws-access-token). Note the 2 of
# 13: a reader of only the LAST round would have seen silence, which is why the script
# accumulates near-miss firings across rounds rather than keeping the final round's.
fires "gitleaks ruleset: the zero-entropy near-miss is given a real key body" \
      "CONTROL FAILED: a NEAR-MISS was flagged" \
      'mutate tools/prove_scanner_fires.py "(\"B\" * 16)" "r(UPPER_NUM, 16)"' \
      run_secrets_canary

# ARM: loopback-regex
# the |localhost branch removed from the alternation
fires "loopback regex: the |localhost alternative is dropped" \
      "CONTROL FAILED: the planted positive pos-name was NOT matched" \
      'mutate_b64 .github/workflows/ci.yml aGl0cz0kKGdyZXAgLW5FICcxMjdcLjBcLjBcLjF8bG9jYWxob3N0JyAiJDEiKSB8fCBzdD0kPw== aGl0cz0kKGdyZXAgLW5FICcxMjdcLjBcLjBcLjEnICIkMSIpIHx8IHN0PSQ/' \
      run_build_loopback
# the backslashes removed, so a dot becomes any character
fires "loopback regex: the dot escapes are dropped" \
      "CONTROL FAILED: the near-miss matched" \
      'mutate_b64 .github/workflows/ci.yml aGl0cz0kKGdyZXAgLW5FICcxMjdcLjBcLjBcLjF8bG9jYWxob3N0JyAiJDEiKSB8fCBzdD0kPw== aGl0cz0kKGdyZXAgLW5FICcxMjcuMC4wLjF8bG9jYWxob3N0JyAiJDEiKSB8fCBzdD0kPw==' \
      run_build_loopback

# ARM: grep-three-way-exit
fires "grep three-way exit: the unreadable target becomes a readable file" \
      "CONTROL FAILED: grep on a directory returned" \
      'mutate .github/workflows/ci.yml "mkdir -p \"\$plant/a-directory\"" ": > \"\$plant/a-directory\"" && reextract' \
      run_build_loopback

# ARM: jq-key-selector
fires "jq key selector: the path is broken" \
      "CONTROL FAILED: the key selector cannot read a key that is present" \
      'mutate .github/workflows/ci.yml ".services.router.environment[\$k] // \"<<absent>>\"" ".services.router.env[\$k] // \"<<absent>>\"" && reextract' \
      run_build_envfile

# ARM: jq-blank-value-selector
fires "jq blank-value selector: the null half is dropped" \
      "CONTROL FAILED: the blank-value selector returned" \
      'mutate .github/workflows/ci.yml "select(.value == \"\" or .value == null)" "select(.value == \"\")" && reextract' \
      run_build_envfile

# ARM: apache-licence-substring
fires "Apache licence substring: narrowed to a word every licence carries" \
      "near-miss matched" \
      'mutate .github/workflows/ci.yml "grep -q \"Apache License\"" "grep -q \"License\"" && reextract' \
      run_build_licence

# ARM: ignore-pattern-probe
fires "ignore-pattern probe: the transferred listing stops listing files" \
      "IGNORE PATTERNS ARE NOT DOING WHAT" \
      'mutate .github/workflows/ci.yml "LC_ALL=C find . -type f" "LC_ALL=C find . -type d" && reextract' \
      run_images_probe

# ARM: ignore-pattern-probe
# The named-state arm for item 4: when the probe CANNOT RUN it must say so under its own
# name, never under the name it uses for a finding. Breaking the capability build is the
# only way to reach that branch on a runner where the exporter works.
fires "ignore-pattern probe: the capability build cannot run" \
      "STATE: BUILDKIT TOO OLD" \
      "mutate .github/workflows/ci.yml 'printf '\"'\"'FROM scratch\\nCOPY . /ctx/\\n'\"'\"' > \"\$cap/df/Dockerfile\"' 'printf '\"'\"'FROM scratch:no-such-tag-exists\\nCOPY . /ctx/\\n'\"'\"' > \"\$cap/df/Dockerfile\"'" \
      run_images_probe

# ARM: router-ignore-patterns
fires "router ignore patterns: reverted to single-segment credential patterns" \
      "backends/.env" \
      'python3 - deploy/images/router/Dockerfile.dockerignore <<PY
import io, sys
p = sys.argv[1]
lines = io.open(p).read().split("\n")
out, n = [], 0
for line in lines:
    if line.startswith("**/") and "__pycache__" not in line and not line.endswith("*.pyc"):
        out.append(line[3:]); n += 1
    else:
        out.append(line)
if n == 0:
    sys.stderr.write("no **/ patterns to revert\n"); sys.exit(2)
io.open(p, "w").write("\n".join(out))
PY' \
      run_images_probe

# ARM: portal-ignore-patterns
fires "portal ignore patterns: reverted to single-segment credential patterns" \
      "src/.env" \
      'python3 - deploy/images/portal/Dockerfile.dockerignore <<PY
import io, sys
p = sys.argv[1]
lines = io.open(p).read().split("\n")
out, n = [], 0
for line in lines:
    if line.startswith("**/"):
        out.append(line[3:]); n += 1
    else:
        out.append(line)
if n == 0:
    sys.stderr.write("no **/ patterns to revert\n"); sys.exit(2)
io.open(p, "w").write("\n".join(out))
PY' \
      run_images_probe

# ARM: jar-pathspec
fires "jar pathspec: narrowed to an extension nothing uses" \
      "CONTROL FAILED: tracked_jars" \
      "mutate .github/workflows/ci.yml \"git ls-files -- '*.jar'\" \"git ls-files -- '*.jarx'\""

# ARM: weights-pathspec
fires "weights pathspec: one format silently dropped" \
      "CONTROL FAILED: tracked_weights" \
      "mutate .github/workflows/ci.yml \"'*.gguf' '*.safetensors'\" \"'*.gguf'\""

# ARM: fonts-pathspec
fires "fonts pathspec: one format silently dropped" \
      "CONTROL FAILED: tracked_fonts" \
      "mutate .github/workflows/ci.yml \"'*.otf' '*.ttf'\" \"'*.otf'\""

# ARM: shebang-index-mode
fires "shebang + index mode: the shebang test no longer matches a shebang" \
      "CONTROL FAILED: expected 2 shebang files" \
      "mutate .github/workflows/ci.yml \"[ \\\"\\\$first2\\\" = '#!' ]\" \"[ \\\"\\\$first2\\\" = '#x' ]\""

# ARM: stray-flat-module-find
fires "stray flat-module find: reverted to the inert -not -path form" \
      "CONTROL FAILED: stray_modules returned" \
      "mutate .github/workflows/ci.yml 'find \"\$sm_r\" -mindepth 2 -name' 'find \"\$sm_r\" -not -path \"\$sm_r/*.py\" -name'"

# ARM: top-level-py-count
fires "top-level .py count: -maxdepth 1 removed" \
      "CONTROL FAILED: top_py_count returned" \
      "mutate .github/workflows/ci.yml 'find \"\$1\" -maxdepth 1 -name' 'find \"\$1\" -name'"

# ARM: py-count
fires "*.py count: narrowed to an extension nothing uses" \
      "CONTROL FAILED: py_count missed a .py under skills/" \
      "mutate .github/workflows/ci.yml 'py_count()     { find \"\$1\" -name '\"'\"'*.py'\"'\"'' 'py_count()     { find \"\$1\" -name '\"'\"'*.pyx'\"'\"''"

# ARM: non-empty-directory
fires "non-empty directory: -type f dropped, so a tree of directories reads non-empty" \
      "CONTROL FAILED: has_no_files called a tree of empty directories non-empty" \
      "mutate .github/workflows/ci.yml 'find \"\$1\" -type f -print -quit' 'find \"\$1\" -print -quit'"

# ARM: credential-shaped-find
fires "credential-shaped find: the .env.* form silently dropped" \
      "CONTROL FAILED: cred_shaped returned" \
      "mutate .github/workflows/ci.yml \"-name '.env' -o -name '.env.*'\" \"-name '.env'\""

# ARM: bare-uvicorn-regex
fires "bare-uvicorn regex: the end anchor dropped, so extras match too" \
      "the bare-uvicorn pattern matched extras" \
      "mutate .github/workflows/ci.yml '([=<>!~;#].*)?\$'\"'\"'' '([=<>!~;#].*)?'\"'\"''"

# ARM: uvicorn-extras-regex
# the escaped bracket removed, so the pattern matches a bare uvicorn line too
fires "uvicorn-extras regex: the escaped bracket dropped, so bare matches too" \
      "the extras pattern matched bare" \
      'mutate_b64 .github/workflows/ci.yml bmFtZXNfdXZpY29ybl9leHRyYXMoKSB7IGdyZXAgLXFpRSAnXltbOnNwYWNlOl1dKnV2aWNvcm5cWycgIiQxIjsgfQ== bmFtZXNfdXZpY29ybl9leHRyYXMoKSB7IGdyZXAgLXFpRSAnXltbOnNwYWNlOl1dKnV2aWNvcm4nICIkMSI7IH0='

# ARM: violation-counter
# back to `wc -l`. The anchor is the whole nz() line because that awk appears
# seven times in this workflow, and an ambiguous anchor is a MUTATION-FAILED rather
# than a mutation -- which is how the first version of this arm reported itself.
fires "violation counter: back to wc -l" "CONTROL FAILED: nz" \
      'mutate_b64 .github/workflows/ci.yml ICAgICAgICAgIG56KCkgICB7IHByaW50ZiAnJXMnICIkMSIgfCB0ciAnfCcgJ1xuJyB8IGF3ayAnTkZ7YysrfSBFTkR7cHJpbnQgYyswfSc7IH0= ICAgICAgICAgIG56KCkgICB7IHByaW50ZiAnJXMnICIkMSIgfCB0ciAnfCcgJ1xuJyB8IHdjIC1sOyB9'

# ARM: violation-printer
# NF && NR>2, which drops a NON-EMPTY entry. The first version used NR>1 and
# FAILED TO FIRE: these lists carry a leading separator, so NR>1 drops only the empty
# first field and still prints three lines for a set of three. The arm found that.
fires "violation printer: drops a line the counter counts" "CONTROL FAILED: show() printed" \
      'mutate_b64 .github/workflows/ci.yml ICAgICAgICAgIHNob3coKSB7IHByaW50ZiAnJXMnICIkMSIgfCB0ciAnfCcgJ1xuJyB8IGF3ayAnTkZ7cHJpbnQgIiAgICAtICIgJDB9JzsgfQ== ICAgICAgICAgIHNob3coKSB7IHByaW50ZiAnJXMnICIkMSIgfCB0ciAnfCcgJ1xuJyB8IGF3ayAnTkYgJiYgTlI+MntwcmludCAiICAgIC0gIiAkMH0nOyB9'

# =======================================================================================
# ONE ARM PER INSTRUMENT IN THE DISCOVERY LIBRARY
# =======================================================================================
# tools/ci/census.sh is the thing that says how many matchers exist. If one of its functions
# silently stops matching, the population reads short and every correspondence passes
# vacuously -- which is the defect this round was written to fix, reappearing inside the fix.
# So each of its nine instruments is broken here and its control must catch the break.

# ARM: marker-spans
fires "marker-spans: the opening marker regex no longer matches a marker" \
      "CONTROL FAILED: marker_spans returned" \
      "mutate tools/ci/census.sh '#[[:space:]]*MATCHER:[[:space:]]*[a-z0-9][a-z0-9-]*[[:space:]]*\$/ {' '#[[:space:]]*MATCHERX:[[:space:]]*[a-z0-9][a-z0-9-]*[[:space:]]*\$/ {'" \
      run_census_controls

# ARM: declared-ids
# the id class narrowed to digits only, so no declared id registers
fires "declared-ids: the id character class no longer matches an id" \
      "CONTROL FAILED: declared_ids MATCHER returned" \
      'mutate_b64 tools/ci/census.sh JHtkaV90YWd9OltbOnNwYWNlOl1dKltBLVphLXowLTldW0EtWmEtejAtOS1dKiI= JHtkaV90YWd9OltbOnNwYWNlOl1dKlswLTldWzAtOV0qIg==' \
      run_census_controls

# ARM: construct-hits
fires "construct-hits: grep and rg drop out of the construct regex" \
      "CONTROL FAILED: construct_hits found" \
      "mutate tools/ci/census.sh '(grep|rg)([[:space:]]|\$)' '(grepx|rgx)([[:space:]]|\$)'" \
      run_census_controls

# ARM: ignore-pattern-files
fires "ignore-pattern-files: the production filename is misspelt" \
      "CONTROL FAILED: ignore_pattern_files returned" \
      "mutate tools/ci/census.sh \"-name 'Dockerfile.dockerignore'\" \"-name 'Dockerfile.dockerignoreX'\"" \
      run_census_controls

# ARM: scanner-sites
fires "scanner-sites: the scanner's own name drops out" \
      "CONTROL FAILED: scanner_sites found" \
      "mutate tools/ci/census.sh '(gitleaks|trufflehog|detect-secrets)' '(gitleaksx|trufflehog|detect-secrets)'" \
      run_census_controls

# ARM: workflow-files
fires "workflow-files: one of the two YAML extensions drops out" \
      "CONTROL FAILED: workflow_files returned" \
      "mutate tools/ci/census.sh \"-name '*.yml' -o -name '*.yaml'\" \"-name '*.ymlx' -o -name '*.yaml'\"" \
      run_census_controls

# ARM: scanned-files
fires "scanned-files: the script extension drops out, so tools/ci is scanned by nothing" \
      "CONTROL FAILED: scanned_files returned" \
      "mutate tools/ci/census.sh \"-name '*.sh' 2>/dev/null\" \"-name '*.shx' 2>/dev/null\"" \
      run_census_controls

# ARM: job-names
fires "job-names: the jobs key is misspelt, so every workflow declares none" \
      "CONTROL FAILED: job_names returned" \
      "mutate tools/ci/census.sh 'doc.get(\"jobs\")' 'doc.get(\"jobz\")'" \
      run_census_controls

# ARM: obligation-ids
fires "obligation-ids: the v() registration form drops out" \
      "CONTROL FAILED: obligation_ids returned" \
      "mutate tools/ci/census.sh '[[:space:]][vd][[:space:]]+' '[[:space:]][xd][[:space:]]+'" \
      run_census_controls

# ARM: exempt-line
# Broken toward the UNSAFE direction: a marker that matches the first line of every file
# truncates the whole census to nothing and reports a clean result. The safe direction --
# failing to match, which un-exempts a file -- needs no arm because it is noisy by itself.
fires "exempt-line: the marker matches the first line of every file" \
      "CONTROL FAILED: exempt_line returned" \
      "mutate tools/ci/census.sh '{ print NR; exit }' '{ print NR; exit }' || true
       python3 - <<PY
import io
p = 'tools/ci/census.sh'
s = io.open(p).read()
old = \"/^[[:space:]]*#[[:space:]]*CENSUS-EXEMPT-BELOW/ { print NR; exit }\"
assert s.count(old) == 1, s.count(old)
io.open(p, 'w').write(s.replace(old, '/./ { print NR; exit }', 1))
PY" \
      run_census_controls

# ARM: says
# Broken OPEN rather than shut, which is the harder direction to catch: a containment test
# that always says yes makes every control above pass. It is caught by the one control that
# asserts a NEGATIVE -- that `uncovered` does not report a construct inside a span.
fires "says: broken open, so it reports every needle as present" \
      "CONTROL FAILED: uncovered reported a construct that IS inside a declared span" \
      "mutate tools/ci/census.sh 'says() { printf '\"'\"'%s'\"'\"' \"\$1\" | grep -qF -- \"\$2\"; }' 'says() { true; }'" \
      run_census_controls

# =======================================================================================
# THE CENSUS JOB'S OWN FOUR DEMONSTRATIONS
# =======================================================================================
# Tagged DEMO, not ARM: these exercise the correspondence LOGIC rather than a matcher, so
# putting them in the arm registry would make each one read as an arm naming no matcher.
# They are what answers "and what arms the census?".

# DEMO: an unarmed matcher must fail the census, by name
fires "DEMO a matcher added with no arm" "deliberately-unarmed" \
      "printf '\n# %s: deliberately-unarmed\nunarmed_probe() { grep -q zzz \"\$1\"; }\n# /%s\n' MATCHER MATCHER >> tools/ci/census.sh" \
      run_census_main

# DEMO: an arm naming nothing must fail the census, by name
fires "DEMO an arm naming a matcher that does not exist" "no-such-matcher" \
      "printf '\n# %s: no-such-matcher\n' ARM >> tools/ci/arms.sh" \
      run_census_main

# DEMO: a pattern outside every span must fail the census, with its file and line
fires "DEMO a pattern-decision construct outside every declared span" \
      "UNDECLARED MATCHER" \
      "mutate .github/workflows/ci.yml 'test -f LICENSE || { echo \"LICENSE missing\"; exit 1; }' 'test -f LICENSE || { echo \"LICENSE missing\"; exit 1; }
          grep -q nothing-at-all /dev/null || true'" \
      run_census_main

# DEMO: deleting a matcher TOGETHER with its arm satisfies set equality, and the floor is
# the only thing that sees it
fires "DEMO a matcher and its arm removed together -- the case set equality cannot see" \
      "POPULATION SHRANK" \
      "python3 - <<PY
import io
w = io.open('.github/workflows/ci.yml').read()
tag = '          # ' + 'MATCHER' + ': violation-printer\n'
assert w.count(tag) == 1, tag
w = w.replace(tag, '', 1)
close = '          # ' + '/MATCHER' + '\n'
i = w.index('show() { printf')
j = w.index(close, i)
w = w[:j] + w[j + len(close):]
io.open('.github/workflows/ci.yml', 'w').write(w)
a = io.open('tools/ci/arms.sh').read()
assert a.count('# ARM: violation-printer\n') == 1
io.open('tools/ci/arms.sh', 'w').write(a.replace('# ARM: violation-printer\n', '', 1))
PY" \
      run_census_main

# =======================================================================================
# THE HARNESS'S OWN POSITIONAL MAP
# =======================================================================================
# The step map decides WHICH STEP every arm above addresses, so if it stops deciding
# correctly the suite keeps printing verdicts about the wrong subjects. That is not a
# hypothetical: a step inserted into `build` ahead of the licence step during this merge
# moved build__08, and the arm went on mutating the new step and judging it -- `caught=3
# MISSED=0`, about something nobody asked it to check. Both halves are armed, because the
# two halves fail differently: one lets an unaddressed id through, the other mis-resolves
# an addressed one.

# ARM: step-map-refs
# A runner addressing build__03, which no map entry names. Mutating the CLONE's arms.sh is
# the point: discovery reads that copy, so this is the same shape as a runner somebody adds
# next month and forgets to map.
fires "step-map-refs: a runner addresses an id that is in no map entry" \
      "ADDRESSED BUT NOT IN THE MAP" \
      'mutate_b64 tools/ci/arms.sh cnVuX2NlbnN1c19tYWluKCkgICAgIHsgcnVuX3N0ZXAgY2Vuc3VzX18wMjsgfQo= cnVuX2NlbnN1c19tYWluKCkgICAgIHsgcnVuX3N0ZXAgY2Vuc3VzX18wMjsgfQpydW5fdW5tYXBwZWRfYXJtKCkgICAgeyBydW5fc3RlcCBidWlsZF9fMDM7IH0K' \
      run_step_map

# ARM: step-map-expectation
# A step inserted into `build` immediately before the licence step -- THE MEASURED FAILURE,
# replayed. Anchors are base64 because both carry leading whitespace that matters: the
# preimage is one `- name: licence and scope invariants` line, the postimage is a dummy step
# in front of it.
fires "step-map-expectation: a step is inserted, shifting build__08 off licence" \
      "RESOLVES TO THE WRONG STEP" \
      'mutate_b64 .github/workflows/ci.yml ICAgICAgLSBuYW1lOiBsaWNlbmNlIGFuZCBzY29wZSBpbnZhcmlhbnRzCg== ICAgICAgLSBuYW1lOiBhIGR1bW15IHN0ZXAgcGxhbnRlZCBieSB0aGUgYXJtCiAgICAgICAgcnVuOiBlY2hvIGR1bW15CiAgICAgIC0gbmFtZTogbGljZW5jZSBhbmQgc2NvcGUgaW52YXJpYW50cwo=' \
      run_step_map

# =======================================================================================
# THE HARNESS'S OWN MATCHER
# =======================================================================================
# ARM: arm-expectation-match
# Not a mutation arm: output_says is what decides every verdict above, so breaking it inside
# a clone would corrupt the judgement rather than test it. Instead it is exercised directly,
# positive and near-miss, which is the same pair every matcher in this repository now carries.
self_arm_expectation() {
  if output_says "the quick brown fox" "a needle that is not there"; then
    record "FAIL-TO-FIRE    | harness: output_says matched a needle that is absent | every arm above would read ARMED whatever happened"
    FAIL=$((FAIL + 1))
    return 0
  fi
  if ! output_says "the quick brown fox" "brown fox"; then
    record "FAIL-TO-FIRE    | harness: output_says missed a needle that is present | every arm above would read WRONG-REASON"
    FAIL=$((FAIL + 1))
    return 0
  fi
  record "FIRES           | harness: output_says -- positive ok, near-miss ok | the verdicts above rest on this"
  PASS=$((PASS + 1))
}
self_arm_expectation

# =======================================================================================
printf '\n================================ ARMS ================================\n'
printf '%s' "$RESULTS"
printf -- '----------------------------------------------------------------------\n'
printf 'as declared: %d    unexpected: %d    skipped by filter: %d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -ne 0 ]; then
  printf '\nEVERY LINE ABOVE THAT IS NOT "FIRES" OR "PASSES-AGAIN" IS A BUG, and which\n'
  printf 'kind it is matters: FAIL-TO-FIRE means the check did not catch its mutation,\n'
  printf 'WRONG-REASON means something else failed instead, and MUTATION-FAILED means\n'
  printf 'nothing was tested at all. The last one is not a pass.\n'
  exit 1
fi
exit 0
