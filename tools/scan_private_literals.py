#!/usr/bin/env python3
# Copyright 2026 The RAMEAU Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Fail the build on a private address or an internal hostname.

WHY THIS EXISTS, AND WHY IT IS NOT gitleaks
-------------------------------------------
CLAUDE.md and README.md both state the rule as: never commit a "credential, key, token,
password, private address or internal hostname". gitleaks enforces the first four. It has
no rule for the last two -- MEASURED, not assumed: a file whose entire content is
`ROUTER_URL = "http://<an RFC1918 address>:8089"` is reported by `gitleaks dir` as
"no leaks found" with exit 0. So for half of the stated rule the number of instruments
covering it was zero, and the one leak this repository has actually had was in that half:
an earlier `shared/test_resolver.py` carried a private /24 and was caught by a human
reading the file, not by a gate.

This script is that missing half. It does not duplicate gitleaks; run both.

THE DESIGN CONSTRAINT THAT SHAPES EVERY RULE BELOW
--------------------------------------------------
A detector for "our hostnames" must not contain our hostnames. Writing the subnet or the
host list into a matcher would publish, as a literal, in a public repository, in the one
file guaranteed to be read, exactly the thing the matcher defends. So:

  * addresses are matched as the generic RFC1918 / CGNAT / link-local RANGES, never as a
    specific subnet; and
  * internal hosts are matched by reserved internal DNS SUFFIX, never by name.

This file therefore contains no complete address literal and no hostname belonging to any
deployment. The address half of that is a test, not a promise:
`tools/test_scan_private_literals.py::test_this_file_contains_no_address_literal`. The
hostname half cannot be tested in public for the same reason it cannot be matched in
public -- a test naming the hosts would publish them -- so it is enforced by review.

If an operator does want to match their own host names, pass them at runtime via
RAMEAU_SCAN_EXTRA_HOSTS (comma-separated). It is deliberately an environment variable and
not a committed file: a committed list is the defect above.

A MEASURED NEGATIVE RESULT: BARE INTERNAL HOSTNAMES ARE NOT DETECTABLE BY SHAPE HERE
-------------------------------------------------------------------------------------
The obvious generic rule for catching a fleet hostname without naming it is "a URL host
with no dot" -- `http://somehost:11434` has no dot, and no public hostname does. It was
implemented, run against this tree, and REMOVED, because in a Docker Compose project it is
unsound and not marginally so:

    http://router:8080   <- correct, required: a compose service name
    http://kb:8090       <- correct, required: a compose service name
    http://somehost:1234 <- a private-network hostname

These are the same shape. `deploy/profiles/compose.env.example` exists precisely to
address services by single-label name inside a container network, and RELEASE-SCOPE.md
records that as deliberate, so the rule fired 23 times on correct content. Narrowing it to
"a single-label host that is not a service declared in deploy/compose.yaml" does not
rescue it either: compose declares two services while the env example and deploy/README.md
legitimately name several more for the profiles, so the legitimate set is not derivable
from the compose file.

So this gate covers ADDRESSES completely and internal HOSTNAMES only where they carry a
reserved suffix or are supplied by the operator at runtime. A bare single-label private
hostname is NOT covered, and no rule here pretends otherwise. That gap is real and is
stated rather than papered over with a rule that would be switched off within a week.

DELIBERATE EXCEPTIONS ARE ANNOTATED IN THE SOURCE, NOT CODED IN HERE
--------------------------------------------------------------------
Some files legitimately need an RFC1918 literal -- the resolver's own tests feed it one to
assert that it is rejected. Those lines carry `rameau-scan: allow` (on the line, or on the
line above). Every exception is then visible in the diff and countable by this script,
rather than living in a skip list here where nobody reviews it. `--audit` prints them.

WHAT IS DELIBERATELY *NOT* FLAGGED
----------------------------------
  * Masked octets such as `192.168.x.4`. They carry no information, and this project uses
    them on purpose -- `shared/resolver.py` exists to refuse them loudly at runtime.
  * `sha256:` digests and lockfile integrity hashes. High-entropy opaque runs in general
    are gitleaks' job; adding them here would make this gate noisy and it would be
    switched off, which is worse than not having it.
  * Loopback and the unspecified address: 127.0.0.0/8, ::1, 0.0.0.0, localhost. The
    release's own `.env.example` is built on loopback by design.
  * The RFC 5737 / RFC 3849 documentation ranges, which exist precisely so that examples
    need not use a real address. They are not RFC1918 and so never match rule R1 anyway;
    `--audit` lists them so a reader can see they were considered.

EXIT CODES
----------
  0  nothing found
  1  findings (the gate fails)
  2  usage or environment error -- NOT a clean result. Never treat 2 as a pass.

Values are never printed. A finding is reported by rule, path, line, column, length and
character class, which is enough to find it and useless as a credential.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# --------------------------------------------------------------------------------------
# Rules. Every pattern here is generic by construction: ranges, not subnets; shapes, not
# names. Adding a literal address or hostname to this list is the one change that would
# defeat the purpose of the file, and the accompanying test fails if anyone does.
# --------------------------------------------------------------------------------------

# Loopback / unspecified / documentation hosts that must never be flagged.
_NEVER_FLAG_HOST = re.compile(
    r"""^(?:
          localhost
        | 127(?:\.\d{1,3}){3}
        | 0\.0\.0\.0
        | \[?::1\]?
        | \[?::\]?
        # RFC 5737 documentation ranges, and RFC 3849 for v6.
        | 192\.0\.2\.\d{1,3}
        | 198\.51\.100\.\d{1,3}
        | 203\.0\.113\.\d{1,3}
        | \[?2001:0?db8:[0-9a-fA-F:]*\]?
      )$""",
    re.X,
)

RULES: list[tuple[str, re.Pattern[str], str]] = [
    # R1 -- the ranges, generically. No subnet of any deployment appears here.
    (
        "rfc1918-literal",
        re.compile(
            r"\b(?:"
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r")\b"
        ),
        "an RFC1918 private address literal",
    ),
    # R2 -- the CGNAT range, which is what Tailscale hands out.
    (
        "cgnat-literal",
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
        "a 100.64/10 CGNAT address literal",
    ),
    # R3 -- link-local.
    (
        "link-local-literal",
        re.compile(r"\b169\.254\.\d{1,3}\.\d{1,3}\b"),
        "a 169.254/16 link-local address literal",
    ),
    # R4 -- reserved internal DNS suffixes. All of these are generic and standardized;
    # none is specific to a deployment. Two exemptions are applied in scan_text():
    #   * host.docker.internal -- Docker's OWN documented name for the host as seen from a
    #     container. It appears 26 times in this tree and in essentially every Docker
    #     project on earth. It names nobody's fleet.
    #   * a dotted path whose first label is a Compose/YAML top-level key, e.g.
    #     `networks.rameau.internal`, which is an attribute path in prose and not a host.
    (
        "internal-dns-suffix",
        re.compile(
            r"\b[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*"
            r"\.(?:ts\.net|local|internal|lan|intranet|corp|home\.arpa)\b",
            re.I,
        ),
        "a hostname on a reserved internal DNS suffix",
    ),
    # R9 -- a user home directory path. Added 2026-11-13, after the gate read clean over a
    # tree carrying the operator's account name in ten files: it was looking for addresses
    # and reserved suffixes, and an account name is neither.
    #
    # THE SHAPE, NOT THE NAME. `\\Users\\<x>` and `/home/<x>` are the two places an account
    # name appears in a path on the platforms this project runs on. Matching the shape is
    # what lets this rule be published; a rule carrying the name would disclose the thing it
    # is meant to prevent, which is the same argument the module header already makes about
    # hostnames.
    #
    # NOT generalized to every drive-absolute path. Counted over this tree before choosing:
    # a `[A-Za-z]:\\...` rule hits 94 times in 35 files, nearly all of them prose about where
    # a file lives, and a rule that needs 94 exemptions teaches the reader to skim them.
    (
        "user-home-path",
        re.compile(
            r"(?:[A-Za-z]:[\\/]Users[\\/]|(?<![A-Za-z0-9_])/(?:home|Users)/)"
            r"(?!(?:Public|Default|All\ Users|Shared)(?![A-Za-z0-9._-]))"
            r"[A-Za-z0-9._-]{1,64}",
            re.I,
        ),
        "a user home directory path, which carries an account name",
    ),
    # R6 -- credentials carried in a URL.
    (
        "url-basic-auth",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]{1,64}:[^/\s@]{1,128}@"),
        "a URL carrying inline credentials",
    ),
    # R7 -- key material.
    (
        "private-key-header",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"),
        "a PEM private key header",
    ),
]

# Docker's own documented host alias, and the YAML key-path false positive. Both are
# exemptions to R4 rather than holes in it: neither names a deployment.
_DOCKER_HOST_ALIAS = re.compile(r"^(?:host\.docker\.internal|gateway\.docker\.internal)$", re.I)
_YAML_KEY_PATH_HEAD = re.compile(
    r"^(?:networks|services|volumes|configs|secrets|x-[a-z0-9-]+)\.", re.I
)

# This project's egress masker emits placeholders of this shape, and its own tests feed
# them back in to assert that re-masking is a no-op. A masked value is the OPPOSITE of a
# leak, so it must not be flagged. (Learned the hard way: a scan that did not know this
# convention reported one of those sentinels as an unexplained 42-char bearer value.)
_MASK_SENTINEL = re.compile(r"^__[A-Z0-9]+(?:_[A-Z0-9]+)*__?$")

_BEARER = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*['\"]?\s*(?:bearer|basic|token)\s+(?P<val>\S{8,})"
)

ALLOW_MARKER = "rameau-scan: allow"

# Rules a BARE marker may not excuse -- it has to name them: `rameau-scan: allow <rule>`.
# allowed_at() reaches up through an entire comment block, which is right for an address
# discussed in surrounding prose and wrong for an account name that merely shares the
# paragraph. Measured 2026-11-13: a marker written for a Docker bridge subnet excused a user
# home path 17 lines below it and the file reported clean.
_STRICT_RULES = frozenset({"user-home-path"})


def _charclass(s: str) -> str:
    if s and all(c in "0123456789abcdefABCDEF" for c in s):
        return "hex"
    bits = []
    if any(c.isupper() for c in s):
        bits.append("upper")
    if any(c.islower() for c in s):
        bits.append("lower")
    if any(c.isdigit() for c in s):
        bits.append("digit")
    if any(c in "+/=" for c in s):
        bits.append("b64sym")
    if any(c in "-_.~:" for c in s):
        bits.append("urlsym")
    return "+".join(bits) or "other"


def _tracked_files(root: str) -> list[str]:
    """The population is what git records, because that is what travels."""
    out = subprocess.run(
        ["git", "-C", root, "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(
            "scan_private_literals: `git ls-files` failed in %s.\n"
            "This gate scans tracked files; with no file list a clean result would prove\n"
            "nothing, so this is an error (exit 2) and not a pass." % root
        )
    # DEDUPLICATED, because `git ls-files` emits a conflicted path ONCE PER MERGE STAGE.
    # Measured 2026-11-03 mid-merge: three unmerged paths produced 40 entries for 34 files
    # and the gate reported NINE findings that were three, each counted three times. Order
    # is preserved so the report still reads in git's order.
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out.stdout.split(b"\0"):
        if not p:
            continue
        rel = p.decode("utf-8", "surrogateescape")
        if rel not in seen:
            seen.add(rel)
            uniq.append(rel)
    return uniq


def _rev_files(root: str, rev: str) -> list[tuple[str, bytes]]:
    """(path, content) for every blob at `rev`, read from git rather than from disk.

    The pre-push hook needs this and the distinction is not pedantic: a push sends
    COMMITTED objects, and a tracked file's working-tree content can differ from what is
    being sent. Scanning the working tree and calling it a gate on the push would be the
    same mis-description this hook's own gitleaks step was corrected for -- it said it
    scanned the worktree when `--source` traverses history.
    """
    listing = subprocess.run(
        ["git", "-C", root, "ls-tree", "-r", "-z", "--format=%(objectname) %(path)", rev],
        capture_output=True, check=False,
    )
    if listing.returncode != 0:
        raise SystemExit(
            "scan_private_literals: `git ls-tree %s` failed. A gate that cannot read its\n"
            "subject must not report success, so this is exit 2 and not a pass." % rev
        )
    out: list[tuple[str, bytes]] = []
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        oid, _, path = entry.partition(b" ")
        blob = subprocess.run(
            ["git", "-C", root, "cat-file", "blob", oid.decode()],
            capture_output=True, check=False,
        )
        if blob.returncode != 0:
            raise SystemExit(
                "scan_private_literals: could not read a blob at %s (exit 2, not a pass)."
                % rev
            )
        out.append((path.decode("utf-8", "surrogateescape"), blob.stdout))
    return out


def _extra_hosts() -> re.Pattern[str] | None:
    raw = os.environ.get("RAMEAU_SCAN_EXTRA_HOSTS", "").strip()
    if not raw:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return None
    return re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names), re.I)


# JS-family sources, where an unquoted dotted name is ambiguous in a way it is not in
# YAML or Python.
_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def _in_quoted_span(line, idx):
    """True when line[idx] sits inside a '...', "..." or `...` span opened on this line.

    Deliberately a LINE scanner and not a parser. It tracks one quote character at a time
    and honours a backslash escape, which is all that is needed to tell a quoted host from
    a bare property access. A template literal opened on an EARLIER line reads as
    unquoted here, and
    that direction is the safe one for a scanner whose job is to over-report: an
    unrecognised string is still scanned, never skipped.
    """
    q = None
    esc = False
    for i, ch in enumerate(line):
        if i >= idx:
            break
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if q is None:
            if ch in "\"'`":
                q = ch
        elif ch == q:
            q = None
    return q is not None


def scan_text(path: str, text: str, extra: re.Pattern[str] | None) -> tuple[list[dict], int]:
    """Return (findings, allowed_count) for one file's text."""
    lines = text.splitlines()
    findings: list[dict] = []
    allowed = 0

    def _marker_rules(line: str) -> tuple[str, ...]:
        """Rule names written after the marker, before any `--` prose tail.

        `rameau-scan: allow`                       -> ()  a bare marker
        `rameau-scan: allow user-home-path -- why` -> ("user-home-path",)

        The `--` tail is how every annotation already in this tree is written, so reading
        the rule list only up to it leaves all of them bare and unchanged.
        """
        tail = line.split(ALLOW_MARKER, 1)[1].split("--", 1)[0]
        return tuple(t for t in tail.replace(",", " ").split() if t)

    def _excuses(line: str, rule: str) -> bool:
        if ALLOW_MARKER not in line:
            return False
        named = _marker_rules(line)
        if rule in named:
            return True
        return not named and rule not in _STRICT_RULES

    def allowed_at(idx: int, rule: str) -> bool:
        """The marker applies to its own line, or to the first code line after the
        contiguous comment block that carries it.

        Not "the previous N lines": a fixed lookback would silently let one marker cover
        unrelated code below it. The comment block is the natural unit -- a reviewer
        reading the annotation sees exactly what it excuses -- and it is bounded by the
        first non-comment, non-blank line going up.

        THE BLOCK REACH IS ALSO THE HAZARD, which is why `rule` is a parameter. A block can
        be long, and a marker written about one finding sits above everything else in it.
        For a rule in _STRICT_RULES the marker must name the rule; for every other rule the
        behaviour is exactly what it was.
        """
        if ALLOW_MARKER in lines[idx] and _excuses(lines[idx], rule):
            return True
        j = idx - 1
        while j >= 0:
            stripped = lines[j].strip()
            if not (stripped == "" or stripped.startswith("#") or stripped.startswith("//")):
                return False
            if ALLOW_MARKER in lines[j] and _excuses(lines[j], rule):
                return True
            j -= 1
        return False

    for i, line in enumerate(lines):
        hits: list[tuple[str, str, int, str]] = []  # (rule, why, col, value)

        for rule, rx, why in RULES:
            for m in rx.finditer(line):
                val = m.group(0)
                if rule == "rfc1918-literal" and _NEVER_FLAG_HOST.match(val):
                    continue
                if rule == "internal-dns-suffix" and (
                    _DOCKER_HOST_ALIAS.match(val) or _YAML_KEY_PATH_HEAD.match(val)
                ):
                    continue
                # A MATCH PRECEDED BY A DOT IS THE TAIL OF A DOTTED FILENAME, NOT A HOST.
                # `\b` opens a match after a leading dot, so a dotenv filename yields a
                # bare `env` + reserved-suffix token  rameau-scan: allow
                # and `.local` is a genuinely reserved suffix (mDNS) -- the rule is right and
                # the token is not a hostname. A DNS name cannot carry an EMPTY first label,
                # which is exactly what a leading dot means, so this cannot exempt a real
                # host: a bare `<name>.local` is not preceded by a dot and still fires, and
                # the unit test asserts exactly that.  rameau-scan: allow
                #
                # FOUND ON THIS MERGE, and only on it: origin/main's step-4 preflight plants
                # `.env.local` fixtures, and this gate arrived from the scrub branch. Neither
                # side alone had both the fixture and the rule, so neither side's CI could
                # have gone red for it -- three findings on a PUBLIC repo's own workflow,
                # every one a false positive. Deliberately NOT fixed by annotating those
                # three lines with `rameau-scan: allow`: the remedy the gate suggests would
                # have hidden a rule defect behind three exemptions, and the next `.env.local`
                # anywhere in the tree would have failed again.
                if rule == "internal-dns-suffix" and m.start() > 0 \
                        and line[m.start() - 1] == ".":
                    continue
                # AN UNQUOTED DOTTED NAME IN A JS-FAMILY SOURCE IS A PROPERTY ACCESS.
                # `metrics.local`, `node.lan`, `props.corp` are ordinary JS  # rameau-scan: allow
                # and name nobody's fleet, but R4 cannot tell them from a real  # rameau-scan: allow
                # `bifrost.lan` by  # rameau-scan: allow
                # shape -- the two are character-for-character the same construct. Until the
                # portal there was no JavaScript in this tree and the rule had never met the
                # case: the first .jsx file scanned produced eight findings, of which FIVE
                # were property accesses.
                #
                # FIXED IN THE RULE RATHER THAN ANNOTATED, for the reason recorded a few
                # lines above about `.env.local`: five `rameau-scan: allow` markers would
                # have described a rule defect as five deliberate literals, and the next
                # .jsx file in the tree would have failed in exactly the same way.
                #
                # THE EXEMPTION IS NARROW AND ITS RESIDUAL IS STATED RATHER THAN ASSERTED
                # AWAY. It applies only to JS-family extensions, and only where the match is
                # NOT inside a quoted span -- so every hostname that could actually be
                # requested still fires, because a host has to be a string before anything
                # can fetch it. What it gives up is a bare hostname written in a `//`
                # comment in a .jsx file. That is prose rather than an endpoint, and it is
                # the price of not flagging every property access in the UI. The tests
                # beside this assert BOTH directions: quoted still fires, unquoted does not.
                if rule == "internal-dns-suffix" \
                        and path.lower().endswith(_JS_SUFFIXES) \
                        and not _in_quoted_span(line, m.start()):
                    continue
                hits.append((rule, why, m.start() + 1, val))

        # R8 -- bearer/basic with an opaque value, excluding this project's mask sentinel.
        for m in _BEARER.finditer(line):
            val = m.group("val").strip("\"'`,;)")
            if _MASK_SENTINEL.match(val):
                continue
            hits.append(("bearer-opaque", "an Authorization header with an opaque value",
                         m.start("val") + 1, val))

        # Optional operator-supplied host names, never committed.
        if extra is not None:
            for m in extra.finditer(line):
                hits.append(("operator-host", "a host named in RAMEAU_SCAN_EXTRA_HOSTS",
                             m.start() + 1, m.group(0)))

        if not hits:
            continue
        # PER HIT, not per line: exemption now depends on the rule, and two rules can fire
        # on one line. Exempting the line as a unit would let a marker naming one rule carry
        # the other one with it -- the same over-reach this change exists to remove.
        kept = []
        for hit in hits:
            if allowed_at(i, hit[0]):
                allowed += 1
            else:
                kept.append(hit)
        for rule, why, col, val in kept:
            findings.append(
                dict(rule=rule, why=why, path=path, line=i + 1, col=col,
                     length=len(val), charclass=_charclass(val))
            )

    return findings, allowed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="scan_private_literals",
        description="Fail on a private address or internal hostname in tracked files.",
    )
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--paths", nargs="*", default=None,
                    help="scan these paths instead of every tracked file")
    ap.add_argument("--audit", action="store_true",
                    help="also list the deliberate `rameau-scan: allow` exemptions")
    ap.add_argument("--rev", default=None,
                    help="scan the committed content at this git rev instead of the "
                         "working tree (what a push actually sends)")
    args = ap.parse_args(argv)

    # `is None` rather than a falsy test, and the distinction is the whole point: an
    # EMPTY --paths means the caller asked for a specific, empty set, which must be an
    # error. A falsy test silently fell back to scanning every tracked file and printed
    # PASS -- a gate reporting success on a subject it was never given. Caught by
    # test_no_files_to_scan_is_an_error_not_a_pass, which is why that test exists.
    if args.rev is not None:
        if args.paths is not None:
            print("scan_private_literals: --rev and --paths are mutually exclusive.")
            return 2
        blobs = _rev_files(args.root, args.rev)
    else:
        paths = _tracked_files(args.root) if args.paths is None else args.paths
        blobs = []
        for rel in paths:
            full = os.path.join(args.root, rel)
            if not os.path.isfile(full):
                continue
            with open(full, "rb") as fh:
                blobs.append((rel, fh.read()))

    if not blobs:
        print("scan_private_literals: no files to scan.")
        print("  A gate that cannot find its subject must not report success.")
        return 2

    extra = _extra_hosts()
    findings: list[dict] = []
    allowed_total = 0
    scanned = 0
    scanned_bytes = 0

    for rel, raw in blobs:
        # Binary files are scanned as latin-1 rather than skipped. Skipping by extension
        # is precisely the gitleaks behavior that leaves a whole file class unread --
        # measured on this repository's nine .woff2 faces, which gitleaks does not read by
        # extension OR by content, and which sit on a path the portal image serves.
        text = raw.decode("utf-8", "replace") if b"\0" not in raw[:8192] \
            else raw.decode("latin-1")
        scanned += 1
        scanned_bytes += len(raw)
        f, a = scan_text(rel, text, extra)
        findings += f
        allowed_total += a

    print("scan_private_literals: source=%s files=%d bytes=%d findings=%d exempted=%d"
          % (("rev:" + args.rev) if args.rev else "worktree",
             scanned, scanned_bytes, len(findings), allowed_total))
    if extra is not None:
        print("  RAMEAU_SCAN_EXTRA_HOSTS is set, so operator-host is active.")

    # Iterate the blobs already read, NOT a `paths` list -- which does not exist in --rev
    # mode. The first version referenced `paths` here and raised UnboundLocalError under
    # `--rev --audit`, which is exactly how the pre-push hook invokes it. The hook then
    # read that crash as "findings" and refused the push citing a private address that
    # was not there: a crash reported as a verdict, and the wrong verdict.
    if args.audit and allowed_total:
        print("  deliberate exemptions carrying `%s`:" % ALLOW_MARKER)
        for rel, raw in blobs:
            text = raw.decode("utf-8", "replace") if b"\0" not in raw[:8192] \
                else raw.decode("latin-1")
            for n, line in enumerate(text.splitlines(), 1):
                if ALLOW_MARKER in line:
                    print("    %s:%d" % (rel, n))

    for f in findings:
        print("  %-24s %s:%d col=%d len=%d class=%s"
              % (f["rule"], f["path"], f["line"], f["col"], f["length"], f["charclass"]))
        print("      %s" % f["why"])

    if findings:
        print("VERDICT: FAIL -- %d private address / internal hostname finding(s)."
              % len(findings))
        print("  No value is printed above, by design. Use path:line to find each one.")
        print("  If a literal is deliberate (a test fixture, say), annotate that line")
        print("  with `%s` so the exception is visible in the diff." % ALLOW_MARKER)
        return 1

    print("VERDICT: PASS -- no private address or internal hostname in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
