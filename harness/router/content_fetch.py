"""Conditional, cached web-page content fetch for grounding enrichment (2026-07-05).

WEBSEARCH-CONTENT-FETCH-LAYER ticket. SearXNG returns snippet listings only; when those
snippets are THIN (lack substance), fetch the actual page text for the top results so the
model has real content to ground in. Deliberately CONDITIONAL (only fires when snippets are
thin) to preserve the fast conversational path, and CACHED (TTL) since factual queries repeat
hosts.

Compliance framework (per ticket §1):
  * robots.txt honored per-domain via urllib.robotparser (stdlib standard parser, NOT hand-rolled;
    Crawlee's BeautifulSoupCrawler was specified but it is a BATCH-crawl framework -- wrong tier for
    fetching 2-3 URLs inside a live request; stdlib robotparser + httpx is the right, dependency-free
    tier and still honors robots properly).
  * per-domain rate limit (>= _MIN_INTERVAL between requests to the same host).
  * real, identifying User-Agent.
  * EXTRACT-FOR-GROUNDING-ONLY: returns substantive text for the model to read; this is NOT a
    verbatim pass-through path to the user response (that copyright-risk shape stays out by design).
  * provenance per fetch: source URL + fetch timestamp + extractor.
No JS rendering (info/news targets, not SPAs) -> no Playwright/Chromium, air-gap-simple.
"""
import os
import re
import time
import asyncio
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

import fetch_guard

_UA = "BifrostRouter-GroundingFetch/1.0 (+self-hosted BIFROST; grounding-only; contact=operator)"
_ROBOTS_TTL = 3600      # re-read robots.txt hourly
_CONTENT_TTL = int(os.getenv("CONTENT_FETCH_TTL_S", "900"))   # cache fetched page text 15 min
_MIN_INTERVAL = 1.5     # per-domain rate limit (seconds)
_FETCH_TIMEOUT = 8.0
_MAX_HOPS = 5        # redirects, walked by hand and re-checked per hop (as POST /fetch)
_REDIRECTS = (301, 302, 303, 307, 308)

_robots: dict = {}      # host -> (RobotFileParser|None, ts)
_content: dict = {}     # url  -> (text, cached_ts, fetched_at_iso)
_last_fetch: dict = {}  # host -> ts


def enabled() -> bool:
    return os.getenv("CONTENT_FETCH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def thin_threshold() -> int:
    """Fetch only when combined snippet content is below this many chars."""
    try:
        return int(os.getenv("CONTENT_FETCH_THIN_CHARS", "500"))
    except ValueError:
        return 500


async def _guarded_get(client, url):
    """GET `url` the way POST /fetch does (main.py _fetch_pinned): every hop resolved and checked
    by fetch_guard, the connection pinned to an address that was checked (Host header + SNI),
    redirects walked by hand. (response, final_url), or None when a hop is refused, every address
    refuses the connection, or the walk runs past _MAX_HOPS. 2026-10-13 audit: this module used
    follow_redirects=True with no address check, so a search result could reach the LAN."""
    for _hop in range(_MAX_HOPS + 1):
        ok, _reason, ips = await asyncio.to_thread(fetch_guard.resolve_fetch_target, url)
        if not ok:
            return None
        target = httpx.URL(url)
        host_header = target.netloc.decode("ascii")
        ext = {"sni_hostname": target.raw_host.decode("ascii")} if target.scheme == "https" else {}
        r = None
        for ip in ips:
            try:
                r = await client.get(target.copy_with(host=str(ip)),
                                     headers={"Host": host_header, "User-Agent": _UA}, extensions=ext)
                break
            except httpx.ConnectError:
                continue
        if r is None:
            return None
        if r.status_code in _REDIRECTS and r.headers.get("location"):
            url = fetch_guard.next_hop(url, r.headers["location"])
            continue
        return r, url
    return None


async def _robots_ok(client, url: str) -> bool:
    p = urlparse(url)
    host = p.netloc
    rp, ts = _robots.get(host, (None, 0.0))
    if time.time() - ts > _ROBOTS_TTL:
        rp = RobotFileParser()
        try:
            # 2026-10-13 audit: was rp.read() -- blocking urllib inside the event loop, no timeout,
            # no address check. Same outcomes as RobotFileParser.read(): 200 parses, 401/403
            # disallow all, any other 4xx allows all, 5xx leaves it unread (can_fetch -> False).
            got = await _guarded_get(client, f"{p.scheme}://{host}/robots.txt")
            if got is None:
                raise OSError("robots.txt unreachable or refused by fetch_guard")
            code = got[0].status_code
            if code == 200:
                rp.parse(got[0].text.splitlines())
            elif code in (401, 403):
                rp.disallow_all = True
            elif 400 <= code < 500:
                rp.allow_all = True
        except Exception:
            rp = None  # robots unreachable -> permissive (logged by caller); conservative alt would skip
        _robots[host] = (rp, time.time())
    if rp is None:
        return True
    try:
        return rp.can_fetch(_UA, url)
    except Exception:
        return True


_STRIP = re.compile(r"(?is)<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<header.*?</header>|<footer.*?</footer>|<!--.*?-->")
_TAGS = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")


def _extract(html: str, limit: int) -> str:
    h = _STRIP.sub(" ", html)
    h = _TAGS.sub(" ", h)
    return _WS.sub(" ", h).strip()[:limit]


async def fetch_top(urls, max_pages: int = 2, per_page_chars: int = 4000) -> list[dict]:
    """Fetch + extract substantive text for up to max_pages of the given URLs.

    Returns [{url, text, fetched_at, extractor}] for pages that fetched cleanly (200, robots-OK,
    non-thin). Skips blocked/disallowed/thin/errored pages. Never raises to the caller path
    (individual failures are swallowed; caller treats [] as "no fetched content").
    """
    out: list[dict] = []
    seen_hosts: set = set()
    for url in urls:
        if len(out) >= max_pages:
            break
        if not url:
            continue
        host = urlparse(url).netloc
        if host in seen_hosts:      # diversify sources; one page per host per call
            continue
        now = time.time()
        c = _content.get(url)
        if c and now - c[1] < _CONTENT_TTL:
            out.append({"url": url, "text": c[0], "fetched_at": c[2], "extractor": "cache"})
            seen_hosts.add(host)
            continue
        try:
            # trust_env=False: an HTTP(S)_PROXY would resolve the name itself and undo the pin.
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False,
                                         trust_env=False) as client:
                if not await _robots_ok(client, url):
                    continue
                wait = _MIN_INTERVAL - (now - _last_fetch.get(host, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                got = await _guarded_get(client, url)
            _last_fetch[host] = time.time()
            if got is None:
                continue
            r = got[0]
            if r.status_code != 200:
                continue
            text = _extract(r.text, per_page_chars)
            if len(text) < 500:     # blocked page / bot-wall / thin -> skip
                continue
            fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _content[url] = (text, time.time(), fetched_at)
            out.append({"url": url, "text": text, "fetched_at": fetched_at, "extractor": "regex-strip"})
            seen_hosts.add(host)
        except Exception:
            continue
    return out
