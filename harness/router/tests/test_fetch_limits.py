"""POST /fetch limits (follow-ups to the 2026-09-15 SSRF packet): the body is streamed and reading STOPS at
main._FETCH_MAX_BYTES; a redirect's body is never read; one deadline bounds the whole redirect walk.

PENDING: coordination/pending/PENDING-router-fetch-limits-2026-09-15. No network: a fixed resolver answer and an
httpx.MockTransport (async handlers and async body streams, so the test can count what was actually read).
"""

import asyncio
import time

import httpx
import pytest

testclient = pytest.importorskip("fastapi.testclient")

import fetch_guard  # noqa: E402
import main  # noqa: E402

PUBLIC = "93.184.215.14"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(fetch_guard, "_default_resolve", lambda host, port: [PUBLIC])
    monkeypatch.setattr(main, "_traf_extract", lambda html, **kw: html)
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)
    monkeypatch.setattr(main.settings, "bifrost_api_key_autopilot", "", raising=False)


def client_for(monkeypatch, handler):
    monkeypatch.setattr(main, "_FETCH_TRANSPORT", httpx.MockTransport(handler))
    return testclient.TestClient(main.app, raise_server_exceptions=False)


def test_body_over_the_cap_is_cut_and_flagged(monkeypatch):
    monkeypatch.setattr(main, "_FETCH_MAX_BYTES", 1000)
    c = client_for(monkeypatch, lambda req: httpx.Response(200, text="x" * 5000))
    r = c.post("/fetch", json={"url": "https://site.test/", "max_chars": 50000})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["body_truncated"] is True
    assert r.json()["length"] == 1000


def test_body_under_the_cap_is_whole_and_not_flagged(monkeypatch):
    monkeypatch.setattr(main, "_FETCH_MAX_BYTES", 1000)
    c = client_for(monkeypatch, lambda req: httpx.Response(200, text="x" * 500))
    r = c.post("/fetch", json={"url": "https://site.test/", "max_chars": 50000})
    assert r.status_code == 200
    assert (r.json()["body_truncated"], r.json()["length"]) == (False, 500)


def test_reading_stops_at_the_cap(monkeypatch):
    monkeypatch.setattr(main, "_FETCH_MAX_BYTES", 1000)
    served = []

    async def endless():
        for i in range(100):
            served.append(i)
            yield b"y" * 100

    c = client_for(monkeypatch, lambda req: httpx.Response(200, content=endless()))
    r = c.post("/fetch", json={"url": "https://site.test/", "max_chars": 50000})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["body_truncated"] is True
    assert len(served) <= 12, len(served)          # 10 chunks fill the cap; not all 100 were pulled


def test_charset_is_honoured_when_decoding(monkeypatch):
    c = client_for(monkeypatch, lambda req: httpx.Response(
        200, content="café".encode("latin-1"), headers={"Content-Type": "text/html; charset=latin-1"}))
    r = c.post("/fetch", json={"url": "https://site.test/"})
    assert r.status_code == 200
    assert r.json()["text"] == "café"


def test_redirect_bodies_are_never_read(monkeypatch):
    read = []

    async def body():
        read.append(1)
        yield b"a large redirect page"

    def handler(req):
        if req.url.path == "/a":
            return httpx.Response(302, headers={"Location": "/b"}, content=body())
        return httpx.Response(200, text="final")

    r = client_for(monkeypatch, handler).post("/fetch", json={"url": "https://site.test/a"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["final_url"] == "https://site.test/b"
    assert read == []


def test_whole_walk_deadline_is_504(monkeypatch):
    monkeypatch.setattr(main, "_FETCH_DEADLINE_CAP_S", 0.5)

    async def slow_loop(req):
        await asyncio.sleep(0.2)
        return httpx.Response(302, headers={"Location": "/r"})

    t0 = time.monotonic()
    r = client_for(monkeypatch, slow_loop).post("/fetch", json={"url": "http://loop.test/r"})
    assert r.status_code == 504, r.text[:300]      # without the deadline: 6 x 0.2 s, then 422 "more than 5"
    assert time.monotonic() - t0 < 3


def test_per_hop_timeout_still_504(monkeypatch):
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    assert client_for(monkeypatch, slow).post("/fetch", json={"url": "https://site.test/"}).status_code == 504
