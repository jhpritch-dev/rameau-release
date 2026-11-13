"""CEREMONY 120: grounding fetch (content_fetch.fetch_top) goes through fetch_guard, as POST /fetch does.

fetch_top fetched search-result URLs with follow_redirects=True and no address check, so a result -- or a
redirect from one -- could reach 127.0.0.1 or the LAN, and the page text was injected into the prompt.
robots.txt was read with blocking urllib (no timeout) inside the event loop. HTTP is a MockTransport;
IP-literal URLs need no DNS. The private-address cases FAIL on b65fed8 (the 2026-10-13 audit's pre-tree run).
"""

import asyncio

import httpx
import pytest

import content_fetch


def _mock_fetch(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(content_fetch.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))
    if not asyncio.iscoroutinefunction(content_fetch._robots_ok):
        # the unpatched module reads robots.txt with blocking urllib (no timeout); keep that off the wire
        monkeypatch.setattr(content_fetch, "_robots_ok", lambda url: True)
    monkeypatch.setattr(content_fetch, "_content", {})
    monkeypatch.setattr(content_fetch, "_robots", {})
    monkeypatch.setattr(content_fetch, "_last_fetch", {})
    return content_fetch


def test_a_redirect_into_a_private_address_is_not_followed(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:11434/api/tags"})
        return httpx.Response(200, text="<p>" + "internal service data " * 60 + "</p>")

    cf = _mock_fetch(monkeypatch, handler)
    out = asyncio.run(cf.fetch_top(["http://8.8.8.8/start"], max_pages=1))
    assert not [u for u in seen if "127.0.0.1" in u], "followed a redirect into 127.0.0.1: %s" % seen
    assert out == []


def test_a_private_result_url_is_not_fetched(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="<p>" + "registry listing " * 60 + "</p>")

    cf = _mock_fetch(monkeypatch, handler)
    out = asyncio.run(cf.fetch_top(["http://10.1.2.3:5000/v2/_catalog"], max_pages=1))  # rameau-scan: allow -- RFC1918 fixture: the LAN address the guard must refuse
    assert not [u for u in seen if "10.1.2.3" in u], "fetched a LAN address: %s" % seen  # rameau-scan: allow -- the needle for the fixture above; must move with it
    assert out == []


def test_a_public_page_still_fetches(monkeypatch):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<p>" + "public article text " * 60 + "</p>")

    cf = _mock_fetch(monkeypatch, handler)
    out = asyncio.run(cf.fetch_top(["http://8.8.8.8/article"], max_pages=1))
    assert len(out) == 1 and "public article text" in out[0]["text"]


def test_a_public_redirect_is_still_followed(monkeypatch):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "8.8.8.8":
            return httpx.Response(301, headers={"location": "http://1.1.1.1/moved"})
        return httpx.Response(200, text="<p>" + "moved article text " * 60 + "</p>")

    cf = _mock_fetch(monkeypatch, handler)
    out = asyncio.run(cf.fetch_top(["http://8.8.8.8/old"], max_pages=1))
    assert len(out) == 1 and "moved article text" in out[0]["text"]


@pytest.mark.parametrize("code,fetched", [(403, False), (404, True), (503, False)])
def test_robots_outcomes_match_the_stdlib_reader(monkeypatch, code, fetched):
    if not asyncio.iscoroutinefunction(content_fetch._robots_ok):
        pytest.skip("the unpatched robots reader is blocking urllib; not exercised offline")
    pages = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(code)
        pages.append(str(request.url))
        return httpx.Response(200, text="<p>" + "article " * 120 + "</p>")

    cf = _mock_fetch(monkeypatch, handler)
    out = asyncio.run(cf.fetch_top(["http://8.8.8.8/a"], max_pages=1))
    assert bool(out) is fetched and bool(pages) is fetched
