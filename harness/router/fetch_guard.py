"""SSRF guard for POST /fetch.

KF1 (Kilo, 2026-09-15, 2AB83D76) with CC fixes from VERDICT-KF1-fetch-guard-2026-09-15:
  D1 a bad port refuses instead of raising;
  D2 clamp_params maps NaN / Infinity / bool / overflow to the default instead of passing or raising;
  D3 IPv6 forms that carry an IPv4 address are judged explicitly, not by the Python version's is_global table
     (3.11 -- the Router's runtime -- calls 6to4 2002:c0a8:204::1 global although it embeds 192.168.x.4).
resolve_fetch_target() also returns the addresses it approved, so the caller can connect to exactly those
(pinning: a second DNS answer -- rebinding -- is never used). Standard library only.
"""

import ipaddress
import math
import socket
from urllib.parse import urljoin, urlparse

# IPv6 ranges that tunnel or translate IPv4: refused outright, whatever the Python version thinks of them.
_V6_REFUSED = (
    ipaddress.ip_network("2002::/16"),       # 6to4
    ipaddress.ip_network("2001::/32"),       # Teredo
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64 (RFC 8215)
    ipaddress.ip_network("::/96"),           # IPv4-compatible (deprecated); also covers :: and ::1
)
# The well-known NAT64 prefix is used by real DNS64 networks: judge it by the IPv4 in its last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_blocked_ip(ip):
    """True if the address must not be fetched: not globally routable, or multicast.

    IPv4-mapped IPv6 and well-known-prefix NAT64 are judged by the IPv4 inside; tunnel forms are refused.
    Anything that is not an ipaddress object is refused (fail closed).
    """
    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _V6_REFUSED):
            return True
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_multicast or not ip.is_global


def resolve_fetch_target(url, resolve=None):
    """(True, "", [ip, ...]) when the Router may fetch `url`, else (False, reason, []).

    resolve(host, port) -> list of str / ipaddress / sockaddr tuple; default socket.getaddrinfo (all results).
    Fail closed: a bad URL, a resolution failure, an empty answer, or ANY blocked address refuses.
    A literal IP host goes through the same rule. The returned list is every address that was checked.
    """
    if not isinstance(url, str):
        return (False, "url is not a str", [])
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (ValueError, TypeError):
        return (False, "malformed url or port", [])
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return (False, "scheme not http/https", [])
    host = parsed.hostname
    if not host:
        return (False, "no host", [])
    if port is None:
        port = 80 if scheme == "http" else 443
    if resolve is None:
        resolve = _default_resolve
    try:
        resolved = resolve(host, port)
    except Exception:
        return (False, "resolution failed", [])
    if not resolved:
        return (False, "resolution returned nothing", [])
    ips = []
    for addr in resolved:
        try:
            ip = _coerce_ip(addr)
        except ValueError:
            return (False, "resolution returned non-IP: %r" % (addr,), [])
        if is_blocked_ip(ip):
            return (False, "blocked address: %s" % ip, [])
        if ip not in ips:
            ips.append(ip)
    return (True, "", ips)


def check_fetch_target(url, resolve=None):
    """(True, "") when the Router may fetch `url`, else (False, reason). See resolve_fetch_target."""
    ok, reason, _ips = resolve_fetch_target(url, resolve)
    return (ok, reason)


def _default_resolve(host, port):
    """socket.getaddrinfo, every IPv4/IPv6 result."""
    out = []
    for family, _stype, _proto, _canon, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if family in (socket.AF_INET, socket.AF_INET6):
            out.append(ipaddress.ip_address(sockaddr[0]))
    return out


def _coerce_ip(addr):
    """A resolved value -> IPv4Address / IPv6Address, or ValueError."""
    if isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return addr
    if isinstance(addr, str):
        return ipaddress.ip_address(addr)
    if isinstance(addr, tuple) and len(addr) >= 1 and isinstance(addr[0], str):
        return ipaddress.ip_address(addr[0])
    raise ValueError("cannot coerce %r" % (addr,))


def _finite_or(value, default):
    """float(value) when it is a finite number (bool is not a number here), else the default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return f if math.isfinite(f) else default


def clamp_params(max_chars, timeout):
    """max_chars -> int in 1..50000 (default 10000); timeout -> float in 1.0..30.0 (default 10.0).

    Missing, non-numeric, bool, NaN or infinite -> the default.
    """
    mc = min(max(int(_finite_or(max_chars, 10000)), 1), 50000)
    to = min(max(_finite_or(timeout, 10.0), 1.0), 30.0)
    return (mc, to)


def next_hop(current_url, location):
    """Absolute URL of a redirect target."""
    return urljoin(current_url, location or "")
