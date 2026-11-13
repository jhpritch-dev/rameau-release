#!/usr/bin/env python3
"""Does every never-block actually deny the shapes it is supposed to deny?

The `# ---- never, under any circumstances ----` block at the end of each
Dockerfile.dockerignore makes three promises, and until 2026-11-03 only one of
them had a step behind it. The block's own comment was candid about that:
SHAPE COVERAGE was "NOT gated, and resting on prose". This is the step. It reads
`deploy/images/NEVER-BLOCK-SHAPES.txt` and requires every pattern there to appear
verbatim in every tracked never-block.

WHAT IT DOES NOT TEST, so a green here is not read as more than it is:

  REACH    -- whether a pattern actually keeps a file out of a built context is a
              property of `docker build`, and nothing in this repository builds
              one. Still ungated. Named here rather than left for a reader to
              notice.
  FINALITY -- that the block is last and nothing re-admits below it belongs to
              ci.yml's `each Dockerfile must still have its .dockerignore beside
              it` step, which decides presence and position in one awk pass. This
              file deliberately does not re-decide either: two spellings of one
              rule invite an edit to half of it.

ON FINDING THE MARKER. This script and that awk pass are, unavoidably, two
readers of the same marker, and ci.yml warns in so many words against letting two
spellings diverge. The normalization here is therefore the same one: strip a
trailing CR, then strip surrounding whitespace, then compare the whole line. If
the two ever disagree about whether a marker exists, one of them goes red, which
is visible -- and that is the honest position rather than a claim that there is
only one reader.

Legs, each of which can fail on its own and says which file and which pattern:

  1. COVERAGE  -- every declared shape appears in every never-block.
  2. DERIVATION -- every credential-class pattern in `.gitignore` has a `**/`
                   twin in the declared set, so the declared set cannot fall
                   behind `.gitignore` without this going red. Negations are
                   skipped; only the three sections named below are read.

Run it with --self-test and it proves both legs can FAIL, on in-memory copies,
before the real check is trusted. That is the same principle as the secret-scan
job's `prove the scanner fires before trusting a zero`, and it is the reason this
file holds pure functions over TEXT rather than paths.
"""

import os
import re
import subprocess
import sys

MARKER = "# ---- never, under any circumstances ----"
DECLARED = "deploy/images/NEVER-BLOCK-SHAPES.txt"
GITIGNORE = ".gitignore"

# The `.gitignore` sections whose shapes a never-block must mirror. Named one by
# one rather than "every section": the operator-config, weights, python, node and
# scratch sections are deliberately NOT mirrored, and a wildcard here would pull
# them in the first time someone added a heading.
CREDENTIAL_SECTIONS = (
    "environment and secrets",
    "keys and certificates",
    "tokens",
)

_SECTION_RE = re.compile(r"^#\s*-{2,}\s*(.*?)\s*-{2,}\s*$")


def norm(line):
    """The same normalization ci.yml's awk pass uses: CR, then surrounding space."""
    return line.rstrip("\r").strip()


def patterns_after_marker(text):
    """The never-block's pattern lines, or None if the marker is not found.

    None and [] are different answers and the caller must tell them apart: a
    missing marker is a structural problem, an empty block is a different one.
    """
    seen = False
    out = []
    for raw in text.splitlines():
        line = norm(raw)
        if not seen:
            if line == MARKER:
                seen = True
            continue
        if line == "" or line.startswith("#"):
            continue
        out.append(line)
    return out if seen else None


def declared_shapes(text):
    return [norm(l) for l in text.splitlines()
            if norm(l) and not norm(l).startswith("#")]


def gitignore_credential_shapes(text):
    """Credential-class `.gitignore` patterns, each as the `**/` form a never-block
    would need. Negations are skipped: `!**/testdata/**/*.key` re-admits."""
    wanted = []
    section = None
    for raw in text.splitlines():
        line = norm(raw)
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            continue
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if section in CREDENTIAL_SECTIONS:
            wanted.append("**/" + line.lstrip("/"))
    return wanted


def check_coverage(declared, blocks):
    """blocks: {path: text}. Returns a list of complaint strings."""
    bad = []
    for path in sorted(blocks):
        pats = patterns_after_marker(blocks[path])
        if pats is None:
            bad.append("%s: no line matched the never-block marker. LOOK BEFORE "
                       "RETYPING IT: if the marker is visibly there, this checker "
                       "is wrong and needs teaching." % path)
            continue
        missing = [p for p in declared if p not in pats]
        if missing:
            bad.append("%s: never-block is missing %d declared shape(s): %s"
                       % (path, len(missing), " ".join(missing)))
    return bad


def check_derivation(declared, gitignore_text):
    missing = [p for p in gitignore_credential_shapes(gitignore_text)
               if p not in declared]
    if not missing:
        return []
    return ["%s names %d credential shape(s) the declared set does not: %s"
            % (GITIGNORE, len(missing), " ".join(missing))]


def tracked_ignore_files():
    out = subprocess.run(["git", "ls-files", "*.dockerignore"],
                         capture_output=True, text=True, check=True).stdout
    # DEDUPLICATED: `git ls-files` emits a conflicted path ONCE PER MERGE STAGE, so during
    # an unresolved merge two ignore files report as six. Measured 2026-11-03 mid-merge --
    # this gate printed "in each of 6 never-block(s)" for two files. The verdict was never
    # wrong (the assertion is per-block presence, so a duplicate is the same file passing
    # twice) but the printed FIGURE was, and a count whose scope is wrong is not a
    # measurement. The same defect was causing a FALSE FAILURE in scan_private_literals.py,
    # where duplicate findings were counted three times over.
    seen, uniq = set(), []
    for p in out.split():
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def self_test():
    """Prove both legs can fail, on copies, before a zero from them is trusted."""
    declared = ["**/*.pem", "**/*.key"]
    ok_block = MARKER + "\n**/*.pem\n**/*.key\n"
    failures = []

    if check_coverage(declared, {"ctl/ok": ok_block}):
        failures.append("control: a complete never-block was reported incomplete")
    if not check_coverage(declared, {"ctl/short": MARKER + "\n**/*.pem\n"}):
        failures.append("control: a never-block MISSING **/*.key was reported complete")
    if not check_coverage(declared, {"ctl/nomarker": "**/*.pem\n**/*.key\n"}):
        failures.append("control: a file with no marker at all was reported fine")
    if not check_coverage(declared, {"ctl/empty": MARKER + "\n"}):
        failures.append("control: an EMPTY never-block was reported complete")

    gi_ok = "# --- tokens ---\n*.pem\n*.key\n"
    gi_new = "# --- tokens ---\n*.pem\n*.key\n*.invented-token\n"
    gi_other = "# --- python ---\n*.pyc\n"
    if check_derivation(declared, gi_ok):
        failures.append("control: a matching .gitignore was reported behind")
    if not check_derivation(declared, gi_new):
        failures.append("control: a NEW credential shape in .gitignore was not noticed")
    if check_derivation(declared, gi_other):
        failures.append("control: a non-credential section was mirrored, which this "
                        "checker must not require")

    # A trailing CR must not hide a present pattern, and must not fake one either.
    if check_coverage(declared, {"ctl/crlf": MARKER + "\r\n**/*.pem\r\n**/*.key\r\n"}):
        failures.append("control: CRLF made a complete never-block read as incomplete")

    if failures:
        print("SELF-TEST FAILED -- this checker cannot be trusted to fire:")
        for f in failures:
            print("  " + f)
        return 1
    print("ok: self-test -- both legs fail when they should, on 8 controls, "
          "including the CRLF case that hides this class on Windows")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()

    root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()
    os.chdir(root)

    declared = declared_shapes(read(DECLARED))
    if not declared:
        print("%s declares no shapes at all. An empty floor passes vacuously, "
              "so this is a failure and not a pass." % DECLARED)
        return 1

    ignores = tracked_ignore_files()
    if not ignores:
        print("no tracked *.dockerignore found. An empty population passes "
              "vacuously, so this is a failure: either the files are untracked, "
              "in which case they never travel, or this checker is looking in "
              "the wrong place.")
        return 1

    bad = check_coverage(declared, {p: read(p) for p in ignores})
    bad += check_derivation(declared, read(GITIGNORE))

    if bad:
        print("never-block shape coverage FAILED:")
        for b in bad:
            print("  " + b)
        print("  The declared floor is %s. Add the shape to the never-block, or "
              "remove it from the floor and say why in that file." % DECLARED)
        return 1

    print("ok: %d declared shape(s) present in each of %d never-block(s), and "
          "%s names none this floor is missing"
          % (len(declared), len(ignores), GITIGNORE))
    # This line used to read "REACH is still ungated: no step here builds a context."
    # It stopped being true on the 2026-11-03 merge, where the `images` job gained a step
    # that builds a PLANTED context and asserts the transferred file list exactly. A gate
    # that prints a stale claim about a sibling control teaches its reader to distrust the
    # part that IS measured, so it states the division of labor instead.
    print("    this gate covers SHAPE COVERAGE only. REACH is gated by \"prove both "
          "ignore files'")
    print("    patterns are in force\" (images job) and LOCATION by A1 of the step-4 "
          "preflight.")
    print("    Residual, and closed by none of the three: `docker build` reads the "
          "WORKING TREE,")
    print("    so a merely gitignored credential is invisible to every git-based gate "
          "here.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
