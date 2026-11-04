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
"""Tests for tools/scan_private_literals.py.

NO PRIVATE ADDRESS IS WRITTEN AS A LITERAL IN THIS FILE, and that is not fussiness.
A test suite proving an address detector works has to feed it addresses, so the obvious
version of this file is full of RFC1918 literals -- and then the gate it tests fails on
it. Measured: the first version of this file produced 18 findings against its own
scanner. Annotating all 18 with `rameau-scan: allow` would have worked and would have
been the wrong fix, because it trains the reader that the marker is boilerplate.

So addresses are ASSEMBLED from octets via `_ip()`, exactly as this project already does
elsewhere -- `KEY = "sk-" + "A" * 24` and `HS_TOKEN = ".".join([...])` in
src/bifrost-router/tests/test_egress_leak_report.py, which carries the note "assembled
from its parts: no JWT-shaped literal in the source". Same reasoning, same technique.

Addresses from the RFC 5737 documentation ranges ARE written plainly, because they exist
precisely to be written down and the detector is required not to flag them.

The test that matters most is `test_neither_file_contains_a_private_address_literal`. A
detector for "our addresses" that contains one of our addresses has published the thing
it defends, and it would pass every other test here while doing so.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scan_private_literals as sut  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _ip(*octets: int) -> str:
    """Assemble a dotted quad so no private-address LITERAL appears in this source.

    See the module docstring: writing a dotted quad out in full here -- even in a comment,
    as an earlier draft of this very docstring did -- makes this file fail the gate it
    tests. The honest fix is not to write the literal rather than to annotate it away.
    """
    return ".".join(str(o) for o in octets)


def scan(text: str, extra=None):
    return sut.scan_text("probe.py", text, extra)


def rules_fired(text: str) -> set[str]:
    findings, _ = scan(text)
    return {f["rule"] for f in findings}


# --------------------------------------------------------------------------------------
# Each rule must be shown able to fire. A rule nobody has seen fire is not a control.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("line,rule", [
    ('URL = "http://%s:8089"' % _ip(10, 11, 12, 13),       "rfc1918-literal"),
    ('URL = "http://%s:8089"' % _ip(172, 20, 1, 2),        "rfc1918-literal"),
    ('URL = "http://%s:8089"' % _ip(192, 168, 99, 250),    "rfc1918-literal"),
    ('PEER = "%s"' % _ip(100, 96, 3, 17),                  "cgnat-literal"),
    ('SELF = "%s"' % _ip(169, 254, 7, 9),                  "link-local-literal"),
    # The five below are NOT addresses, so `_ip()` cannot help and assembling them from
    # fragments would only make them unreadable. They are annotated instead, one marker
    # per line so each is explicit: every one is a generic illustrative value that names
    # no host and no deployment, and the key material is a header with no key after it.
    ('HOST = "somebox.ts.net"',   "internal-dns-suffix"),      # rameau-scan: allow
    ('HOST = "nas.lan"',          "internal-dns-suffix"),      # rameau-scan: allow
    ('HOST = "printer.home.arpa"', "internal-dns-suffix"),     # rameau-scan: allow
    ('DSN = "postgres://someuser:somepass@db.example.com:5432/app"',  # rameau-scan: allow
     "url-basic-auth"),
    ('-----BEGIN OPENSSH PRIVATE KEY-----', "private-key-header"),  # rameau-scan: allow
])
def test_each_rule_fires(line, rule):
    assert rule in rules_fired(line), "%s did not fire on %r" % (rule, line)


def test_the_boundaries_of_each_range_are_right():
    """An off-by-one in a range is a silent hole, so the edges get their own test.

    172.15 and 172.32 are PUBLIC and must not fire; 172.16 and 172.31 are private and
    must. Same for the CGNAT range's 100.63 / 100.64 / 100.127 / 100.128 edges.
    """
    for public in (_ip(172, 15, 1, 1), _ip(172, 32, 1, 1),
                   _ip(100, 63, 1, 1), _ip(100, 128, 1, 1), _ip(11, 1, 1, 1)):
        assert rules_fired('U = "%s"' % public) == set(), "%s is public" % public
    for private in (_ip(172, 16, 1, 1), _ip(172, 31, 1, 1)):
        assert "rfc1918-literal" in rules_fired('U = "%s"' % private), private
    for cgnat in (_ip(100, 64, 1, 1), _ip(100, 127, 1, 1)):
        assert "cgnat-literal" in rules_fired('U = "%s"' % cgnat), cgnat


def test_the_measured_gap_is_real_and_unclaimed():
    """A bare single-label host is NOT covered, and the docstring says so.

    This is a test that an honest limitation stays honest: if someone adds a
    single-label-host rule back, this fails and they must also update the prose that
    currently tells a reader the gap exists.
    """
    assert rules_fired('URL = "http://somebox:11434"') == set()
    assert "NOT covered" in sut.__doc__


# --------------------------------------------------------------------------------------
# Exemptions. Each one is a place the gate stays quiet; each therefore needs a test, or
# it is indistinguishable from a rule that never worked.
# --------------------------------------------------------------------------------------

def test_loopback_and_documentation_addresses_are_never_flagged():
    for ok in ('"http://127.0.0.1:11434"', '"http://localhost:8089"',
               '"http://192.0.2.4:8089"', '"http://198.51.100.7:80"',
               '"http://203.0.113.9:443"', '"http://0.0.0.0:8080"'):
        assert rules_fired("U = %s" % ok) == set(), ok


def test_docker_host_alias_is_not_an_internal_hostname():
    """host.docker.internal is Docker's own documented name and names nobody's fleet.

    It appears 26 times in this repository. Without this exemption the gate fails on
    correct content, and a gate that fails on correct content gets switched off.
    """
    assert rules_fired('extra_hosts: ["host.docker.internal:host-gateway"]') == set()


def test_a_yaml_key_path_is_not_a_hostname():
    """`networks.rameau.internal` is a Compose attribute path written in prose."""
    assert rules_fired("set networks.rameau.internal to true") == set()


def test_the_projects_mask_sentinel_is_not_a_bearer_leak():
    """A MASKED value is the opposite of a leak.

    This exemption exists because a scan that did not know the convention reported one of
    these sentinels as an unexplained opaque bearer value, in
    src/bifrost-router/tests/test_egress_leak_report.py, and a human had to read the line
    to find out it was the egress masker's own output being fed back in.
    """
    assert rules_fired("Authorization: Bearer __BIFROST_REDACTED_1__") == set()
    # ... while a real opaque value on the same header still fires.
    opaque = "Zm9vYmFyYmF6cXV1eA" + "7" * 20
    assert "bearer-opaque" in rules_fired("Authorization: Bearer %s" % opaque)


# --------------------------------------------------------------------------------------
# The annotation mechanism. It is the only way to exempt a line, so its SCOPE has to be
# exact: too narrow and nobody can annotate a fixture, too wide and one marker silently
# excuses unrelated code below it.
# --------------------------------------------------------------------------------------

_A = _ip(10, 1, 1, 1)
_B = _ip(10, 2, 2, 2)
_C = _ip(192, 168, 4, 4)


def test_marker_on_the_same_line_exempts_it():
    f, a = scan('U = "http://%s:80"  # rameau-scan: allow' % _A)
    assert f == [] and a == 1


def test_marker_in_a_comment_block_reaches_the_first_code_line():
    f, a = scan(
        "# rameau-scan: allow -- fixture\n"
        "# second line of the same block\n"
        "\n"
        'U = "http://%s:80"\n' % _A
    )
    assert f == [] and a == 1


def test_marker_does_NOT_reach_past_an_intervening_code_line():
    """The bound that makes the mechanism reviewable.

    Found by running the gate against this repository: a four-line annotation above
    `env = {...}` did not, and must not, also excuse the `assert` on the line after it.
    """
    f, a = scan(
        "# rameau-scan: allow -- fixture\n"
        + 'env = "http://%s:80"\n' % _A      # exempted: first code line after the block
        + 'again = "http://%s:80"\n' % _B    # NOT exempted: a code line intervened
    )
    assert a == 1
    assert [x["line"] for x in f] == [3]


def test_an_unannotated_literal_still_fails_in_the_same_file():
    f, _ = scan(
        "# rameau-scan: allow\n"
        + 'ok = "http://%s:80"\n' % _A
        + "\n"
        + 'leaked = "http://%s:8089"\n' % _C
    )
    assert [x["rule"] for x in f] == ["rfc1918-literal"]
    assert [x["line"] for x in f] == [4]


# --------------------------------------------------------------------------------------
# Reporting discipline and exit codes.
# --------------------------------------------------------------------------------------

def test_a_finding_never_carries_the_value():
    addr = _ip(10, 11, 12, 13)
    f, _ = scan('URL = "http://%s:8089"' % addr)
    assert f, "expected a finding"
    for key, val in f[0].items():
        assert addr not in str(val), "the value leaked into field %r" % key
    assert set(f[0]) == {"rule", "why", "path", "line", "col", "length", "charclass"}


def test_operator_supplied_hosts_are_opt_in_and_uncommitted():
    """The escape hatch for real host names is an env var, never a file in the repo."""
    assert rules_fired('node = "somebox"') == set()
    extra = sut._extra_hosts()
    assert extra is None or os.environ.get("RAMEAU_SCAN_EXTRA_HOSTS")
    rx = re.compile(r"\bsomebox\b", re.I)
    assert "operator-host" in {f["rule"] for f in scan('node = "somebox"', rx)[0]}
    src = open(os.path.join(HERE, "scan_private_literals.py"), encoding="utf-8").read()
    assert 'RAMEAU_SCAN_EXTRA_HOSTS", ""' in src, \
        "the default must be empty, i.e. no host list is committed"


def test_audit_works_in_rev_mode(capsys):
    """`--rev` together with `--audit` must not crash.

    It did. The audit block iterated a `paths` list that only exists in worktree mode, so
    `--rev HEAD --audit` raised UnboundLocalError -- and that is precisely how
    tools/git-hooks/pre-push invokes it. The hook then read the crash's nonzero exit as
    "findings" and refused the push citing a private address that was not there. A crash
    reported as a verdict, and the wrong verdict; this test is why it cannot return.
    """
    rc = sut.main(["--root", REPO, "--rev", "HEAD", "--audit"])
    out = capsys.readouterr().out
    assert rc in (0, 1), "a crash is not a verdict (rc=%r)" % rc
    assert "VERDICT:" in out, "no verdict line, so the run did not conclude"
    assert "Traceback" not in out


def test_rev_and_paths_are_mutually_exclusive():
    assert sut.main(["--root", REPO, "--rev", "HEAD", "--paths", "README.md"]) == 2


def test_rev_mode_reads_committed_content_not_the_worktree(capsys):
    """The distinction the hook depends on: a push sends objects, not files on disk."""
    sut.main(["--root", REPO, "--rev", "HEAD"])
    assert "source=rev:HEAD" in capsys.readouterr().out


def test_no_files_to_scan_is_an_error_not_a_pass():
    """A gate that cannot find its subject must not report success."""
    assert sut.main(["--root", REPO, "--paths"]) == 2


def test_the_repository_itself_passes():
    """Arm 1 of the proof, as a test rather than a one-off command."""
    assert sut.main(["--root", REPO]) == 0


# --------------------------------------------------------------------------------------
# THE ONE THAT MATTERS: the detector must not embed what it defends.
# --------------------------------------------------------------------------------------

_QUAD = re.compile(r"(?<![\d.\\])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")


@pytest.mark.parametrize("name", ["scan_private_literals.py",
                                  "test_scan_private_literals.py"])
def test_neither_file_contains_a_private_address_literal(name):
    """Neither the detector NOR its tests may contain a PRIVATE address literal.

    This is the defect the whole design is arranged against: a matcher written as a
    specific subnet publishes that subnet, as a literal, in a public repository, in the
    file most certain to be read. It would pass every other test here while doing it.

    Ranges expressed as escaped regex (`10\\.\\d{1,3}...`) are not complete literals and
    are what this file is supposed to contain. Loopback, the unspecified address and the
    RFC 5737 documentation ranges are allowed, because they exist precisely to be written
    down and they identify nobody.
    """
    source = open(os.path.join(HERE, name), encoding="utf-8").read()
    offenders = []
    for quad in _QUAD.findall(source):
        try:
            ip = ipaddress.IPv4Address(quad)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified:
            continue
        if any(ip in ipaddress.IPv4Network(n) for n in
               ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")):
            continue
        cgnat = ipaddress.IPv4Network(_ip(100, 64, 0, 0) + "/10")
        if ip.is_private or ip.is_link_local or ip in cgnat:
            offenders.append(quad)
    assert not offenders, (
        "%s contains private address literal(s) %r. In the detector, express the RANGE "
        "as an escaped regex; in the tests, build the address with _ip(). A concrete "
        "private address in either file is published, and it is the thing these files "
        "exist to keep out." % (name, offenders)
    )


def test_the_tracked_tree_is_checked_by_git_not_a_glob():
    """The population is `git ls-files`, because that is what travels to the remote."""
    out = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, text=True)
    assert out.returncode == 0
    assert set(sut._tracked_files(REPO)) == set(out.stdout.split())
