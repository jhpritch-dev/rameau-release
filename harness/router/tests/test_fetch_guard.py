"""fetch_guard.py: the SSRF guard for POST /fetch (KF1 + CC fixes D1-D3, VERDICT-KF1-fetch-guard-2026-09-15).

Pure unit tests: fake resolvers only, no network. The D1/D2/D3/pinning cases FAIL on Kilo's original KF1 module
(2AB83D76) -- the scratch-tree run demonstrates that.
"""

import ipaddress
import math
import socket

import pytest

import fetch_guard as fg


def fake(*ips):
    return lambda host, port: list(ips)


def boom(host, port):
    raise socket.gaierror(socket.EAI_NONAME, "name not resolved")


PUBLIC = "93.184.215.14"


# --- KF1's contract (Kilo's cases) ---------------------------------------------------------------------------

def test_public_name_allowed():
    assert fg.check_fetch_target("https://example.org/", fake(PUBLIC)) == (True, "")


def test_name_resolving_private_refused():
    ok, reason = fg.check_fetch_target("http://internal.test/", fake("192.168.0.4"))  # rameau-scan: allow -- RFC1918 fixture fed to the guard's blocklist; the test proves it is REFUSED
    assert ok is False and "192.168.0.4" in reason  # rameau-scan: allow -- RFC1918 fixture fed to the guard's blocklist; the test proves it is REFUSED


def test_any_private_answer_refuses_the_name():
    assert fg.check_fetch_target("http://mixed.test/", fake(PUBLIC, "10.0.0.5"))[0] is False  # rameau-scan: allow -- RFC1918 fixture in the guard's blocklist test


@pytest.mark.parametrize("ip", ["127.0.0.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "169.254.169.254",  # rameau-scan: allow -- link-local fixture in the guard's blocklist test
                                "10.1.2.3", "172.16.0.1", "192.168.0.4", "::1", "fd00::1", "fe80::1",  # rameau-scan: allow -- RFC1918 fixtures in the guard's blocklist test (two on this line)
                                "::ffff:127.0.0.1", "::ffff:192.168.0.4", "ff02::1"])  # rameau-scan: allow -- RFC1918 fixture fed to the guard's blocklist; the test proves it is REFUSED
def test_blocked_addresses(ip):
    assert fg.is_blocked_ip(ipaddress.ip_address(ip)) is True


@pytest.mark.parametrize("ip", [PUBLIC, "2606:2800:21f:cb07:6820:80da:af6b:8b2c", "::ffff:93.184.215.14"])
def test_public_addresses(ip):
    assert fg.is_blocked_ip(ipaddress.ip_address(ip)) is False


def test_non_ip_is_blocked():
    assert fg.is_blocked_ip("127.0.0.1") is True


def test_literal_mapped_v6_refused_with_real_resolver():
    assert fg.check_fetch_target("http://[::ffff:127.0.0.1]/")[0] is False


@pytest.mark.parametrize("resolver", [boom, lambda h, p: None, lambda h, p: [], fake("not-an-ip"),
                                      lambda h, p: 1 / 0])
def test_resolution_failures_fail_closed(resolver):
    assert fg.check_fetch_target("http://x.test/", resolver)[0] is False


@pytest.mark.parametrize("url", ["ftp://example.org/", "https:///path-only", "javascript:alert(1)", "", None, 42])
def test_bad_scheme_or_no_host_refused(url):
    assert fg.check_fetch_target(url, fake(PUBLIC))[0] is False


def test_redirect_hop_is_resolved_then_checked():
    hop = fg.next_hop("https://example.org/a", "http://127.0.0.1/admin")
    assert hop == "http://127.0.0.1/admin"
    assert fg.check_fetch_target(hop)[0] is False


def test_scheme_relative_redirect_keeps_scheme_and_is_checked():
    hop = fg.next_hop("https://example.org/a", "//127.0.0.1/x")
    assert hop == "https://127.0.0.1/x"
    assert fg.check_fetch_target(hop)[0] is False


def test_relative_redirect_resolves_against_current_url():
    assert fg.next_hop("https://example.org/a/b", "../c") == "https://example.org/c"


@pytest.mark.parametrize("args,want", [(("abc", None), (10000, 10.0)), ((10 ** 9, 999), (50000, 30.0)),
                                       ((0, 0), (1, 1.0)), ((None, None), (10000, 10.0)),
                                       (("", ""), (10000, 10.0)), (("2500", "2.5"), (2500, 2.5))])
def test_clamp_params(args, want):
    assert fg.clamp_params(*args) == want


# --- D1: a bad port refuses (KF1 raised ValueError) ------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://example.org:99999/", "http://example.org:abc/", "http://[::1/"])
def test_d1_bad_port_or_url_refuses_without_raising(url):
    ok, reason = fg.check_fetch_target(url, fake(PUBLIC))
    assert ok is False and reason


# --- D2: non-finite / bool params fall back to the default (KF1 passed NaN, raised on Infinity) ----------------

@pytest.mark.parametrize("mc,to", [(float("nan"), float("nan")), (float("inf"), float("inf")),
                                   (float("-inf"), float("-inf")), (True, False), ("Infinity", "NaN"),
                                   (10 ** 400, 10 ** 400)])
def test_d2_non_finite_and_bool_params_get_defaults(mc, to):
    got_mc, got_to = fg.clamp_params(mc, to)
    assert (got_mc, got_to) == (10000, 10.0)
    assert math.isfinite(got_to)


# --- D3: IPv6 forms that embed IPv4 (KF1 allowed these on the Router's Python 3.11) ----------------------------

@pytest.mark.parametrize("ip", ["2002:c0a8:204::1",          # 6to4 around 192.168.x.4
                                "2002:5db8:d70e::1",         # 6to4 around a PUBLIC v4: tunnels refused outright
                                "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
                                "64:ff9b::7f00:1",           # NAT64 around 127.0.0.1
                                "64:ff9b::c0a8:204",         # NAT64 around 192.168.x.4
                                "64:ff9b:1::1",              # local-use NAT64
                                "::127.0.0.1", "::"])        # IPv4-compatible, unspecified
def test_d3_embedded_ipv4_forms_blocked(ip):
    assert fg.is_blocked_ip(ipaddress.ip_address(ip)) is True


def test_d3_nat64_around_a_public_v4_is_allowed():
    assert fg.is_blocked_ip(ipaddress.ip_address("64:ff9b::5db8:d70e")) is False


def test_d3_name_with_6to4_private_answer_refused():
    assert fg.check_fetch_target("http://sixto4.test/", fake("2002:c0a8:204::1"))[0] is False


# --- pinning support: resolve_fetch_target hands back exactly what it checked ----------------------------------

def test_resolve_returns_the_checked_addresses_once_each():
    ok, reason, ips = fg.resolve_fetch_target("https://example.org/", fake(PUBLIC, PUBLIC, "2606:2800::1"))
    assert (ok, reason) == (True, "")
    assert ips == [ipaddress.ip_address(PUBLIC), ipaddress.ip_address("2606:2800::1")]


def test_resolve_refusal_returns_no_addresses():
    assert fg.resolve_fetch_target("http://x.test/", fake(PUBLIC, "10.0.0.5")) == (  # rameau-scan: allow -- RFC1918 fixture in the guard's blocklist test
        False, "blocked address: 10.0.0.5", [])  # rameau-scan: allow -- RFC1918 fixture in the guard's blocklist test


def test_resolve_calls_the_resolver_once_per_check():
    calls = []

    def counting(host, port):
        calls.append((host, port))
        return [PUBLIC]

    fg.resolve_fetch_target("https://example.org:8443/x", counting)
    assert calls == [("example.org", 8443)]
