"""POST /fetch wiring: resolve once per hop, refuse any private answer, PIN the connection to the approved address,
walk at most 5 redirects by hand (John 2026-09-15: "go, stage the /fetch wiring with D1-D3 and pinning").

PENDING: coordination/pending/PENDING-router-fetch-ssrf-pinning-2026-09-15. No network: fetch_guard's resolver is
replaced by a fake DNS table and main._FETCH_TRANSPORT by an httpx.MockTransport, so every request the endpoint
makes is recorded -- which address it went to, which Host header and which TLS SNI name it carried.
"""

import httpx
import pytest

testclient = pytest.importorskip("fastapi.testclient")

import fetch_guard  # noqa: E402
import main  # noqa: E402

PUBLIC = "93.184.215.14"
PUBLIC6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
PAGE = "<html><title>T</title><body>hello</body></html>"


class FakeNet:
    """A DNS table + a web server keyed by (Host header, path)."""

    def __init__(self):
        self.dns = {}        # name -> list of answers, or list of lists (one per lookup: rebinding)
        self.lookups = []
        self.requests = []
        self.routes = {}     # (host header, path) -> callable(request) -> httpx.Response
        self.refuse_connect = set()

    def resolve(self, host, port):
        self.lookups.append(host)
        answers = self.dns[host]
        if answers and isinstance(answers[0], list):
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return answers

    def handler(self, request):
        if request.url.host in self.refuse_connect:
            raise httpx.ConnectError("refused", request=request)
        self.requests.append(request)
        route = self.routes.get((request.headers.get("host"), request.url.path))
        return route(request) if route else httpx.Response(200, text=PAGE)


@pytest.fixture
def net(monkeypatch):
    n = FakeNet()
    monkeypatch.setattr(fetch_guard, "_default_resolve", n.resolve)
    monkeypatch.setattr(main, "_FETCH_TRANSPORT", httpx.MockTransport(n.handler))
    monkeypatch.setattr(main, "_traf_extract", lambda html, **kw: "EXTRACTED " + html)
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)
    monkeypatch.setattr(main.settings, "bifrost_api_key_autopilot", "", raising=False)
    return n


@pytest.fixture
def client(net):
    return testclient.TestClient(main.app, raise_server_exceptions=False)


def redirect(location, code=302):
    return lambda req: httpx.Response(code, headers={"Location": location})


def test_public_name_is_fetched_pinned_to_the_checked_address(net, client):
    net.dns["site.test"] = [PUBLIC]
    r = client.post("/fetch", json={"url": "https://site.test/page"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["final_url"] == "https://site.test/page"
    assert r.json()["text"].startswith("EXTRACTED ")
    (req,) = net.requests
    assert req.url.host == PUBLIC                          # connected to the address the guard approved
    assert req.headers["host"] == "site.test"              # the server still sees the name
    assert req.extensions["sni_hostname"] == "site.test"   # TLS verifies the certificate against the name


def test_explicit_port_is_kept_in_url_and_host_header(net, client):
    net.dns["site.test"] = [PUBLIC]
    assert client.post("/fetch", json={"url": "http://site.test:8080/x"}).status_code == 200
    (req,) = net.requests
    assert (req.url.host, req.url.port, req.headers["host"]) == (PUBLIC, 8080, "site.test:8080")
    assert "sni_hostname" not in req.extensions            # plain http: no TLS


def test_rebinding_second_answer_is_never_used(net, client):
    net.dns["rebind.test"] = [[PUBLIC], ["127.0.0.1"]]    # first lookup public, every later one loopback
    r = client.post("/fetch", json={"url": "http://rebind.test/"})
    assert r.status_code == 200, r.text[:300]
    assert net.lookups == ["rebind.test"]                 # one resolution for the one hop
    assert [q.url.host for q in net.requests] == [PUBLIC]


def test_name_resolving_private_is_refused_before_any_request(net, client):
    net.dns["internal.test"] = ["192.168.0.4"]  # rameau-scan: allow -- RFC1918 fixture fed to the guard's blocklist; the test proves it is REFUSED
    r = client.post("/fetch", json={"url": "http://internal.test/"})
    assert r.status_code == 422
    assert "private/local URLs not allowed" in r.json()["error"]
    assert net.requests == []


def test_one_private_answer_among_public_refuses(net, client):
    net.dns["mixed.test"] = [PUBLIC, "10.0.0.5"]  # rameau-scan: allow -- RFC1918 fixture: the DNS answer the guard must refuse
    assert client.post("/fetch", json={"url": "http://mixed.test/"}).status_code == 422
    assert net.requests == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                                 "http://169.254.169.254/latest/meta-data/"])  # rameau-scan: allow -- link-local fixture: the cloud metadata address the guard must refuse
def test_literal_private_addresses_refused(net, client, url):
    ip = url.split("//")[1].split("/")[0].strip("[]")
    net.dns[ip] = [ip]
    assert client.post("/fetch", json={"url": url}).status_code == 422
    assert net.requests == []


def test_redirect_to_loopback_is_refused_after_one_request(net, client):
    net.dns["site.test"] = [PUBLIC]
    net.dns["127.0.0.1"] = ["127.0.0.1"]
    net.routes[("site.test", "/a")] = redirect("http://127.0.0.1/admin")
    r = client.post("/fetch", json={"url": "https://site.test/a"})
    assert r.status_code == 422
    assert "redirect hop 1" in r.json()["error"]
    assert len(net.requests) == 1


def test_redirect_to_a_name_resolving_private_is_refused(net, client):
    net.dns["site.test"] = [PUBLIC]
    net.dns["intranet.test"] = ["10.1.2.3"]  # rameau-scan: allow -- RFC1918 fixture: the DNS answer the guard must refuse
    net.routes[("site.test", "/a")] = redirect("http://intranet.test/")
    assert client.post("/fetch", json={"url": "https://site.test/a"}).status_code == 422
    assert len(net.requests) == 1


def test_relative_redirect_resolves_against_the_name_not_the_ip(net, client):
    net.dns["site.test"] = [PUBLIC]
    net.routes[("site.test", "/a")] = redirect("/b", 301)
    r = client.post("/fetch", json={"url": "https://site.test/a"})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["final_url"] == "https://site.test/b"
    assert [(q.url.host, q.headers["host"], q.url.path) for q in net.requests] == [
        (PUBLIC, "site.test", "/a"), (PUBLIC, "site.test", "/b")]


def test_more_than_five_redirects_refused(net, client):
    net.dns["loop.test"] = [PUBLIC]
    net.routes[("loop.test", "/r")] = redirect("/r", 307)
    r = client.post("/fetch", json={"url": "http://loop.test/r"})
    assert r.status_code == 422
    assert "more than 5 redirects" in r.json()["error"]
    assert len(net.requests) == 6                          # the first request + 5 followed redirects


def test_connect_failure_moves_to_the_next_checked_address(net, client):
    net.dns["dual.test"] = [PUBLIC, PUBLIC6]
    net.refuse_connect.add(PUBLIC)
    r = client.post("/fetch", json={"url": "https://dual.test/"})
    assert r.status_code == 200, r.text[:300]
    assert [q.url.host for q in net.requests] == [PUBLIC6]


def test_every_address_refusing_connect_is_502(net, client):
    net.dns["down.test"] = [PUBLIC]
    net.refuse_connect.add(PUBLIC)
    assert client.post("/fetch", json={"url": "https://down.test/"}).status_code == 502


def test_timeout_is_504(net, client):
    net.dns["slow.test"] = [PUBLIC]

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    net.routes[("slow.test", "/")] = slow
    assert client.post("/fetch", json={"url": "https://slow.test/"}).status_code == 504


def test_resolution_failure_is_422(net, client):
    assert client.post("/fetch", json={"url": "https://nxdomain.test/"}).status_code == 422
    assert net.requests == []


@pytest.mark.parametrize("url", ["http://site.test:99999/", "http://site.test:abc/"])
def test_bad_port_is_422_not_500(net, client, url):
    net.dns["site.test"] = [PUBLIC]
    assert client.post("/fetch", json={"url": url}).status_code == 422


@pytest.mark.parametrize("body", [{"url": 42}, {"url": None}, {"url": "   "}, {}])
def test_missing_or_non_string_url_is_422_not_500(net, client, body):
    r = client.post("/fetch", json=body)
    assert r.status_code == 422
    assert r.json() == {"error": "url required"}


@pytest.mark.parametrize("extra", [{"max_chars": "abc", "timeout": "soon"}, {"max_chars": None, "timeout": True},
                                   {"max_chars": [1], "timeout": {}}])
def test_bad_params_fall_back_to_defaults_not_500(net, client, extra):
    net.dns["site.test"] = [PUBLIC]
    r = client.post("/fetch", json=dict({"url": "https://site.test/"}, **extra))
    assert r.status_code == 200, r.text[:300]


def test_non_finite_json_params_do_not_500(net, client):
    net.dns["site.test"] = [PUBLIC]
    r = client.post("/fetch", content=b'{"url": "https://site.test/", "max_chars": Infinity, "timeout": NaN}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code in (200, 422), r.text[:300]


def test_max_chars_is_clamped_and_truncates(net, client):
    net.dns["site.test"] = [PUBLIC]
    r = client.post("/fetch", json={"url": "https://site.test/", "max_chars": 0})
    assert r.status_code == 200
    assert r.json()["text"].startswith("E\n\n[...truncated]")


def test_scheme_check_is_unchanged(net, client):
    r = client.post("/fetch", json={"url": "ftp://site.test/"})
    assert r.status_code == 422
    assert r.json() == {"error": "scheme must be http or https"}
