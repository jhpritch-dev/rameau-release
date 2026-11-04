#!/usr/bin/env python3
"""Prove the secret scanner fires, across several credential shapes, before a zero
from the real scan is believed.

WHY THIS EXISTS, AND WHY IT GENERATES RATHER THAN COMMITS
---------------------------------------------------------
A clean scan report is exactly the output nobody re-checks, so it has to be earned. The
only way to earn it is to plant a known-positive and confirm the scanner reports it.

Every canary here is GENERATED from random material at run time, for two reasons that
are both load-bearing:

  1. A literal credential written into this file would itself be a finding, and the real
     scan step scans the whole workspace -- so the gate would fail on its own source.
     Allowlisting this path instead would punch a hole in the one layer that runs
     unconditionally.
  2. The first version of this control used AWS's documented example key, which gitleaks
     ALLOWLISTS by default precisely because it appears in documentation everywhere. The
     control failed on its first run and correctly refused to let the real scan be
     trusted. A canary must be a value the scanner will actually flag.

Canaries are written to a scratch directory OUTSIDE the repository. Nothing is written
into the workspace, and no value here is a real credential.

WHY THE SHAPE SET IS MEASURED, NOT ASSERTED
-------------------------------------------
Scanner rules change between versions. Measured 2026-11-03 against gitleaks 8.30.1: the
AWS access-key shape -- the single shape this control used to plant -- does NOT fire any
more, in any variant tried (bare id, id beside a secret, quoted assignment, ASIA prefix).
Nine other shapes did fire. But this repository's CI pins 8.21.2, so that measurement was
taken on a DIFFERENT INSTRUMENT than the one that runs here, and a shape list copied from
it would be a number measured with the wrong tool.

So this script does not hard-code which shapes must fire. It plants every candidate,
prints the per-shape matrix, and enforces two things:

  TIER 1 (always): at least one shape must fire. This is the irreducible claim -- the
          scanner is alive and configured, so a zero from the real scan means something.
          If nothing fires, the scan below cannot be trusted and the job fails.

  TIER 2 (once a floor file exists): every shape named in tools/scanner-shape-floor.txt
          must fire. That file is a FLOOR, not a pinned total: shapes may be added as
          coverage grows, and a floor does not go red merely because the candidate list
          got longer. It starts empty on purpose -- the first CI run on the PINNED
          scanner version prints the matrix, and the floor is filled in from that
          evidence rather than from a probe run elsewhere.

A shape that stops firing is a real loss of coverage and is reported by name, so an
upgrade that drops a rule shows up as "this class is no longer detected" instead of as a
mystery.

Usage:
    prove_scanner_fires.py [--json] [--floor PATH]
"""

import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile

UPPER_NUM = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits
B64ISH = ALNUM + "+/"
URLSAFE = ALNUM + "-_"
HEX = "0123456789abcdef"

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FLOOR = os.path.join(_HERE, "scanner-shape-floor.txt")
# The repository's own gitleaks config, one level up from tools/.
CONFIG = os.path.join(os.path.dirname(_HERE), ".gitleaks.toml")


def r(alphabet, n):
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _pem_banner(word):
    """Build a PEM banner at run time instead of writing one as a literal.

    Measured while building this gate: spelling the banner out in full made THIS FILE
    match gitleaks' `private-key` rule, so the workspace scan two steps below went red
    on the gate's own source -- the self-reference the generated-canary design exists to
    avoid, reintroduced through the one shape whose marker is a fixed string rather than
    random material. Splitting the fragments keeps the source clean while the canary
    written to the scratch directory is still a byte-exact banner.
    """
    return "-----%s OPENSSH " % word + "PRIVATE " + "KEY-----"


def _draws():
    """[(shape, filename, content)] for ONE draw -- all generated, none real.

    The set spans the credential classes this stack actually risks: forge/host tokens,
    cloud API keys, a private key, and a high-entropy assignment. It is deliberately
    wider than any one scanner's rule set, because the point is to find out what the
    pinned scanner covers, not to assume it.

    Called afresh for every draw, so each retry plants new random material.
    """
    return [
        ("aws-access-key",
         "creds.txt", "aws_access_key_id = AKIA%s\n" % r(UPPER_NUM, 16)),
        ("github-pat",
         "gh.txt", "token: ghp_%s\n" % r(ALNUM, 36)),
        ("github-fine-grained-pat",
         "gh2.txt", "token: github_pat_%s_%s\n" % (r(ALNUM, 22), r(ALNUM, 59))),
        ("gitlab-pat",
         "gl.txt", "GITLAB_TOKEN=glpat-%s\n" % r(URLSAFE, 20)),
        ("slack-bot-token",
         "slack.txt", "SLACK_BOT_TOKEN=xoxb-%s-%s-%s\n"
         % (r(string.digits, 13), r(string.digits, 13), r(ALNUM, 24))),
        ("stripe-secret",
         "stripe.txt", "STRIPE_KEY=sk_live_%s\n" % r(ALNUM, 24)),
        ("gcp-api-key",
         "g.txt", "GOOGLE_API_KEY=AIza%s\n" % r(URLSAFE, 35)),
        ("openai-project-key",
         "oa.txt", "OPENAI_API_KEY=sk-proj-%s\n" % r(ALNUM, 48)),
        ("anthropic-key",
         "an.txt", "ANTHROPIC_API_KEY=sk-ant-api03-%s\n" % r(URLSAFE, 93)),
        ("private-key-pem",
         "id_canary", _pem_banner("BEGIN") + "\n"
         + "\n".join(r(B64ISH, 64) for _ in range(6))
         + "\n" + _pem_banner("END") + "\n"),
        ("hex-entropy-assignment",
         "h.txt", 'api_secret = "%s"\n' % r(HEX, 64)),
    ]


def _near_misses():
    """[(name, filename, content)] -- key-SHAPED non-keys, and prose naming no value.

    THE NEGATIVE HALF OF THIS CONTROL, and without it the positive half is only
    half a proof. A matcher satisfied by something that flags EVERYTHING is as
    blind as one satisfied by something that flags nothing: both let the real
    scan's zero go unchecked, and both print a reassuring line while doing it.
    So the same scan that proves the scanner CAN see a credential must also
    prove it does NOT see these.

    DELIBERATELY DETERMINISTIC, AND THAT IS A CORRECTION RATHER THAN A TASTE.
    These are planted in EVERY round, so a near-miss with any entropy at all
    would eventually clear gitleaks' entropy threshold on some draw and red the
    build at random -- the exact flake this file's retry design exists to
    remove, arriving by the opposite door. Built by character REPETITION rather
    than written out, for the same reason the positives are generated: no
    key-shaped literal may enter a file in this repository.

    Each one differs from a real key in exactly ONE property, which is what
    makes it a near-miss rather than merely a non-key:
      nearmiss-short       correct prefix and alphabet, fails on LENGTH (8 body)
      nearmiss-lowentropy  correct prefix, alphabet AND length, fails on ENTROPY
                           alone -- the sharper of the two
      nearmiss-prose       names the vocabulary of secrets and no value at all

    MEASURED CLEAN, with a generated positive in the same repository and the
    same invocation, on gitleaks 8.30.1 + this repo's .gitleaks.toml: 12 rounds,
    zero near-miss findings, while the positive fired in 2 of the 12 (which is
    itself consistent with aws-access-key's known intermittency). The 8.21.2
    reading is CI's to take, and this gate is the thing that takes it: if a
    near-miss fires on the pinned version it goes red and names which, rather
    than quietly becoming an over-broad control.
    """
    return [
        ("nearmiss-short", "nearmiss-short.txt",
         "aws_access_key_id = AKIA%s\n" % ("A" * 8)),
        ("nearmiss-lowentropy", "nearmiss-flat.txt",
         "aws_access_key_id = AKIA%s\n" % ("B" * 16)),
        ("nearmiss-prose", "plain.txt",
         "prose about keys and tokens and passwords, naming no value\n"),
    ]


def _write(d, filename, content):
    with open(os.path.join(d, filename), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write(content)


def scan_round(pending):
    """Plant ONE fresh draw of every still-undetected shape, in ONE scratch repo,
    and scan it ONCE. Returns {shape: [rule ids]} for whatever fired this round.

    One scan per round rather than one per shape. The per-shape loop it replaced cost
    a scan for every shape on every draw, so an undetected shape alone drove 24 git
    inits, commits and scans per run -- enough to make this step the slowest thing in
    the job for no added proof. Attribution is by FILENAME, which is why the filenames
    must be distinct, and that is asserted rather than assumed: a collision would
    silently merge two shapes' results and report a shape as covered on another
    shape's finding.
    """
    d = tempfile.mkdtemp(prefix="canary_")
    try:
        by_file = {}
        nm_file = {}
        for shape in pending:
            filename, content = draw_for(shape)
            if filename in by_file:
                raise RuntimeError(
                    "two shapes share the filename %r (%s and %s) -- attribution "
                    "is by filename, so this would credit one shape with the "
                    "other's finding" % (filename, by_file[filename], shape))
            by_file[filename] = shape
            _write(d, filename, content)
        # The near-misses go into the SAME repository and the SAME scan as the
        # positives. Separately scanned near-misses would be a different
        # experiment -- specificity has to be measured under the conditions the
        # positive was measured under, or it speaks for a scan nobody ran.
        for name, filename, content in _near_misses():
            if filename in by_file or filename in nm_file:
                raise RuntimeError(
                    "near-miss %s wants the filename %r, which is already "
                    "planted -- attribution is by filename, so a collision "
                    "would credit a positive's finding to a near-miss or the "
                    "reverse, and the control would invert silently"
                    % (name, filename))
            nm_file[filename] = name
            _write(d, filename, content)
        subprocess.run(["git", "init", "-q", "."], cwd=d, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=d, capture_output=True)
        subprocess.run(["git", "-c", "user.email=ci@example.invalid",
                        "-c", "user.name=ci", "commit", "-qm", "canary"],
                       cwd=d, capture_output=True)
        rep = os.path.join(d, "report.json")
        argv = ["gitleaks", "detect", "--source", d, "--no-banner", "--redact",
                "--report-format", "json", "--report-path", rep, "--exit-code", "1"]
        # Use THIS REPOSITORY'S config, not the default ruleset.
        #
        # The canaries are planted in a scratch directory outside the repo, and gitleaks
        # auto-loads .gitleaks.toml from the SOURCE root -- which here is the scratch
        # directory, so the repo's config would never be read. The control would then
        # exercise a different ruleset than the scan two steps later: a control that does
        # not share its subject's configuration cannot speak for it. Passing --config
        # explicitly is what makes this proof about the scan that actually runs.
        if os.path.exists(CONFIG):
            argv += ["--config", CONFIG]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
        # The REPORT is the evidence, not the exit code. gitleaks exits non-zero when it
        # finds something, so for canaries a non-zero exit is the expected case -- but a
        # non-zero exit could equally be a crash, and an exit code cannot tell a finding
        # from an epitaph. If the report is missing or unparseable, that is reported as a
        # round that established nothing, never as a round where nothing fired.
        if not os.path.exists(rep):
            raise RuntimeError(
                "gitleaks wrote no report (exit %d). This round proves nothing either "
                "way; an empty result is not an absence. stderr: %s"
                % (proc.returncode, (proc.stderr or "").strip()[:300]))
        try:
            with open(rep, encoding="utf-8") as fh:
                findings = json.load(fh) or []
        except (ValueError, OSError) as exc:
            raise RuntimeError("gitleaks report unparseable: %s" % exc)

        fired = {}
        nm_fired = {}
        for f in findings:
            base = os.path.basename((f.get("File") or "").replace("\\", "/"))
            shape = by_file.get(base)
            if shape:
                fired.setdefault(shape, set()).add(f.get("RuleID", "?"))
                continue
            nm = nm_file.get(base)
            if nm:
                nm_fired.setdefault(nm, set()).add(f.get("RuleID", "?"))
        return ({s: sorted(v) for s, v in fired.items()},
                {n: sorted(v) for n, v in nm_fired.items()})
    finally:
        shutil.rmtree(d, ignore_errors=True)


# How many independent draws a shape gets before it is called undetected.
#
# WHY RETRIES ARE NOT A FUDGE. Measured 2026-11-03 on gitleaks 8.30.1: the AWS
# access-key shape fired on only 7 of 20 generated draws -- 35 %. Entropy does not
# explain it (fired draws spanned 3.406-3.875 bits over the random tail, missed draws
# 3.149-3.875, fully overlapping, with equal-entropy draws on both sides), so some
# draws of a correctly-shaped key are simply not reported -- gitleaks' own stopword and
# allowlist handling is the likely cause, and the mechanism was NOT established.
#
# That makes a SINGLE planted canary an intermittent control, and an intermittent
# control is disqualifying: it either reds the build at random or teaches a reader to
# wave the failure through, which is how the real scan's zero quietly stops being
# checked.
#
# The claim being proved is "this scanner CAN detect this shape", and ONE firing draw
# settles that. So the right treatment for an intermittent subject is re-draw, never
# localisation -- an intermittent fault is neither monotonic nor stationary, so trying
# to pin down WHICH draws fail would produce a crisp and worthless answer.
#
# The draw count is set from the measured rate, not chosen for looking generous. At 35 %
# per draw, ten draws leave a 1.3 % chance of calling a covered shape dead -- and that
# tail was not theoretical: it hit on the very next run while this gate was being built,
# with aws-access-key missing all ten draws and the matrix reporting 9 of 11. Twenty-four
# draws put it near 6e-5. Only a genuinely undetected shape pays the full count; a covered
# one stops at its first hit, and the attempt count is printed so a shape that needed
# many draws stays visible instead of hiding behind a pass.
DRAWS_PER_SHAPE = 24

# Shapes whose per-draw detection rate is measured to be WELL UNDER 1, so a miss says
# nothing about coverage. Keeping this explicit is the difference between "the scanner
# cannot see this class" and "this draw was unlucky", and the output must not conflate
# them.
#
# aws-access-key, measured 2026-11-03 on gitleaks 8.30.1, pooled across runs: roughly
# 10 of 32 draws, about 31 %. Confirmed NOT to be a config effect by a paired test --
# the same twelve generated keys scored 3/12 with the default ruleset and 3/12 with this
# repository's .gitleaks.toml, so `[extend] useDefault = true` suppresses nothing. The
# mechanism was never established; entropy was tested and refuted.
KNOWN_INTERMITTENT = {
    "aws-access-key": "~31 % per draw (10 of 32), mechanism unestablished; "
                      "entropy tested and refuted; not a config effect (paired 3/12 vs 3/12)",
}


def shape_names():
    return [row[0] for row in _draws()]


def draw_for(shape):
    """One fresh draw of the named shape."""
    for name, filename, content in _draws():
        if name == shape:
            return filename, content
    raise KeyError(shape)


def scan_all():
    """Round-robin until every shape has fired or the draw budget is spent.

    Returns rows in the declared shape order, each with the round it fired on.
    """
    rows = {s: {"shape": s, "flagged": False, "rules": [], "attempts": 0,
                "draws_allowed": DRAWS_PER_SHAPE} for s in shape_names()}
    pending = shape_names()
    nm_hits = {}
    rounds_run = 0
    for round_no in range(1, DRAWS_PER_SHAPE + 1):
        if not pending:
            break
        rounds_run = round_no
        fired, nm_fired = scan_round(pending)
        # A near-miss firing in ANY round is a specificity failure, so the hit is
        # accumulated rather than overwritten -- the last round's silence must not
        # erase an earlier round's firing.
        for name, rules in nm_fired.items():
            rec = nm_hits.setdefault(name, {"rounds": 0, "rules": set()})
            rec["rounds"] += 1
            rec["rules"].update(rules)
        still = []
        for shape in pending:
            if shape in fired:
                rows[shape].update(flagged=True, rules=fired[shape],
                                   attempts=round_no)
            else:
                rows[shape]["attempts"] = round_no
                still.append(shape)
        pending = still
    nm_out = {n: {"rounds": v["rounds"], "rules": sorted(v["rules"])}
              for n, v in nm_hits.items()}
    return ([rows[s] for s in shape_names()],
            (DRAWS_PER_SHAPE if pending else None), nm_out, rounds_run)


def read_floor(path):
    """Shapes that MUST fire. Missing or all-comments file means no floor yet."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def main(argv):
    floor_path = (argv[argv.index("--floor") + 1]
                  if "--floor" in argv else DEFAULT_FLOOR)

    ver = subprocess.run(["gitleaks", "version"], capture_output=True, text=True)
    version = (ver.stdout or ver.stderr or "?").strip()

    rows, _exhausted, nm_hits, nm_rounds = scan_all()
    fired = [x["shape"] for x in rows if x["flagged"]]
    dead = [x["shape"] for x in rows if not x["flagged"]]
    floor = read_floor(floor_path)
    floor_missing = [s for s in floor if s not in fired]
    floor_unknown = [s for s in floor
                     if s not in {x["shape"] for x in rows}]

    if "--json" in argv:
        print(json.dumps({"gitleaks": version, "rows": rows, "fired": fired,
                          "dead": dead, "floor": floor,
                          "floor_missing": floor_missing,
                          "near_misses": [n for n, _, _ in _near_misses()],
                          "near_miss_rounds": nm_rounds,
                          "near_miss_firings": nm_hits}, indent=2))
    else:
        print("gitleaks version: %s" % version)
        print("%-26s %-6s %-9s %s"
              % ("shape", "FIRED", "draws", "rule ids"))
        for x in rows:
            print("%-26s %-6s %-9s %s"
                  % (x["shape"], "yes" if x["flagged"] else "NO",
                     "%d/%d" % (x["attempts"], x["draws_allowed"]),
                     ",".join(x["rules"]) or "-"))
        print("\n%d of %d candidate shapes detected within %d draws each."
              % (len(fired), len(rows), DRAWS_PER_SHAPE))
        print("specificity: %d near-misses planted in each of %d round(s); %s"
              % (len(_near_misses()), nm_rounds,
                 "none fired" if not nm_hits
                 else "FIRED: %s" % ", ".join(sorted(nm_hits))))
        noisy = [x for x in rows if x["flagged"] and x["attempts"] > 1]
        if noisy:
            print("Shapes that needed more than one draw (the per-draw rate is "
                  "well under 1, so this is expected, not a fault):")
            for x in noisy:
                print("  - %s took %d draws" % (x["shape"], x["attempts"]))
        if dead:
            gaps = [s for s in dead if s not in KNOWN_INTERMITTENT]
            flaky = [s for s in dead if s in KNOWN_INTERMITTENT]
            if gaps:
                print("NOT detected in %d draws -- a real coverage gap for this "
                      "scanner version and config:" % DRAWS_PER_SHAPE)
                for s in gaps:
                    print("  - %s" % s)
            if flaky:
                print("Missed, but KNOWN INTERMITTENT -- this is NOT a coverage "
                      "statement:")
                for s in flaky:
                    print("  - %s (%s)" % (s, KNOWN_INTERMITTENT[s]))

    # TIER 1 -- the irreducible claim.
    if not fired:
        print("\nCONTROL FAILED: not one generated known-positive was flagged.")
        print("The scanner is not configured as expected, so a zero from the real")
        print("scan proves nothing. Failing on purpose.")
        return 1

    # TIER 1b -- SPECIFICITY. The other half of tier 1, and it fails the job just
    # as hard. Tier 1 establishes that the scanner CAN see a planted credential;
    # on its own that is satisfied by a matcher which flags every file it is
    # shown, and such a matcher makes the real scan's zero meaningless while
    # printing "control ok". So a near-miss firing is a control failure, not a
    # curiosity, and it is reported with the rule that fired so the next reader
    # starts from the rule rather than from the shape.
    if nm_rounds == 0:
        print("\nCONTROL FAILED: no round ran, so the near-misses were never")
        print("scanned. A specificity claim over zero rounds is vacuous, and an")
        print("empty result is not an absence. Failing rather than reporting ok.")
        return 1
    if nm_hits:
        print("\nCONTROL FAILED: a NEAR-MISS was flagged. The matcher is wider")
        print("than the shape it claims to match, so a finding from the real scan")
        print("below cannot be read as a credential -- and a zero cannot be read")
        print("as cleanliness either, because this control no longer distinguishes")
        print("them. Which fired, and on what rule:")
        for name in sorted(nm_hits):
            print("  - %s fired in %d of %d round(s), rule(s): %s"
                  % (name, nm_hits[name]["rounds"], nm_rounds,
                     ",".join(nm_hits[name]["rules"]) or "?"))
        print("Measured CLEAN on gitleaks 8.30.1 with this repo's config, so a")
        print("firing here means the pinned version differs from that reading --")
        print("record the version and the rule before changing the near-miss, and")
        print("do NOT weaken it to get green: an over-broad scanner is the finding.")
        return 1

    # TIER 2 -- the declared floor, once one exists.
    if floor_unknown:
        print("\nFLOOR NAMES A SHAPE THIS SCRIPT DOES NOT PLANT: %s"
              % ", ".join(floor_unknown))
        print("A floor entry that matches no candidate can never fail, which makes")
        print("the floor look satisfied while proving nothing. Fix the name.")
        return 1
    # A floor must contain only shapes whose detection is DETERMINISTIC. Allowing a
    # known-intermittent shape onto it would turn this gate into a flake generator --
    # red at random, which is exactly the failure the retry design was added to remove.
    floor_flaky = [s for s in floor if s in KNOWN_INTERMITTENT]
    if floor_flaky:
        print("\nFLOOR NAMES A KNOWN-INTERMITTENT SHAPE: %s"
              % ", ".join(floor_flaky))
        for s in floor_flaky:
            print("  %s: %s" % (s, KNOWN_INTERMITTENT[s]))
        print("A floor entry must be deterministically detected, or this gate goes red")
        print("at random and teaches its readers to ignore it. Remove it from the floor.")
        return 1

    if floor_missing:
        print("\nCOVERAGE REGRESSION: shapes on the declared floor did not fire:")
        for s in floor_missing:
            print("  - %s" % s)
        print("Either this scanner version dropped the rule, or the planted shape")
        print("drifted from what the rule matches. Both are real; neither is noise.")
        print("Floor file: %s" % floor_path)
        return 1

    if not floor:
        print("\nNo declared floor yet (%s is empty)." % floor_path)
        print("Tier 1 passed, so the scan below can be trusted. Fill the floor from")
        print("the matrix ABOVE -- it was measured on the scanner version this CI")
        print("actually pins, which is the only measurement that counts here.")
    else:
        print("\nfloor satisfied: %s" % ", ".join(floor))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
