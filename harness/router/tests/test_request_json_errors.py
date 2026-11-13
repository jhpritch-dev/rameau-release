"""A malformed or non-object JSON request body gets HTTP 400 invalid_json, not an unhandled 500.

PENDING: coordination/pending/PENDING-router-malformed-json-400-2026-09-11.
Seen on v434 (OUTCOME-router-v434-build-and-rollout-2026-09-11 s.3): a curl body whose quotes
PS5.1 had stripped reached POST /v1/chat/completions; `await request.json()` raised
JSONDecodeError ("Expecting property name enclosed in double quotes: line 2 column 5 (char 7)")
and the Router answered 500 with a starlette traceback.

Groups:
  J1  handlers that parsed the body BARE: malformed, empty, non-UTF-8, absurdly nested and
      non-object bodies all get 400 {"error": {"type": "invalid_request_error",
      "code": "invalid_json"}}.
  J2  handlers that already caught a parse error keep their own error shape; a non-object
      body (which used to 500 on body.get) now gets that same answer.
  J3  the WARNING log line names the path and never carries the body; nor does the response.
  J4  SCOPE: a JSONDecodeError raised while HANDLING a request (e.g. parsing a backend
      response) is NOT turned into a 400. These pass before AND after the change on purpose.
  J5  a valid object body still reaches the handler unchanged.

Every request here is answered before any backend is contacted. TestClient is not used as a
context manager, so the lifespan (poll tasks, http client) never runs.
"""

import json
import logging

import pytest

testclient = pytest.importorskip("fastapi.testclient")

import main  # noqa: E402

CONTROL = "test-control-token"
V434_BODY = b'{\r\n    model: "bifrost-auto",\r\n    messages: []\r\n}'
V434_POS = "line 2 column 5 (char 7)"

MALFORMED = {
    "v434_unquoted_keys": V434_BODY,
    "truncated": b'{"model": "bifrost-auto", "messages": [',
    "empty": b"",
    "whitespace_only": b"  \r\n ",
    "not_utf8": b'{"model": "\xff\xfe"}',
    "deeply_nested": b"[" * 200000,
}
NON_OBJECT = {
    "array": b"[]",
    "string": b'"hello"',
    "null": b"null",
    "number": b"42",
    "boolean": b"true",
}
ALL_BAD = {**MALFORMED, **NON_OBJECT}

# Handlers that called `await request.json()` with no guard (500 before this change).
BARE = [
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/classify",
    "/strategy",
    "/mode",
    "/fleet/load",
    "/autopilot/submit",
    "/search",
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)
    monkeypatch.setattr(main.settings, "bifrost_api_key_autopilot", "", raising=False)
    monkeypatch.setattr(main, "_CONTROL_TOKEN", CONTROL)
    return testclient.TestClient(main.app, raise_server_exceptions=False)


def _post(client, path, body):
    return client.post(path, content=body,
                       headers={"Content-Type": "application/json", "X-Bifrost-Control": CONTROL})


# ---------------------------------------------------------------------------
# J1 -- bare handlers: 400 invalid_json
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(ALL_BAD))
@pytest.mark.parametrize("path", BARE)
def test_j1_bad_body_is_400_invalid_json(client, path, kind):
    r = _post(client, path, ALL_BAD[kind])
    assert r.status_code == 400, (path, kind, r.status_code, r.text[:200])
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "invalid_json"
    assert isinstance(err["message"], str) and 0 < len(err["message"]) < 200
    if kind in NON_OBJECT:
        assert "must be a JSON object" in err["message"]
    if kind in ("empty", "whitespace_only"):
        assert "empty" in err["message"]


def test_j1_v434_body_reports_the_parse_position(client):
    r = _post(client, "/v1/chat/completions", V434_BODY)
    assert r.status_code == 400
    msg = r.json()["error"]["message"]
    assert "Expecting property name enclosed in double quotes" in msg
    assert V434_POS in msg


# ---------------------------------------------------------------------------
# J2 -- handlers that already caught the parse keep their own shape
# ---------------------------------------------------------------------------

def _count_tokens_shape(r):
    j = r.json()
    return r.status_code == 400 and j["type"] == "error" and j["error"]["type"] == "invalid_request_error"


def _messages_shape(r):
    return r.status_code == 400 and r.json() == {"error": "invalid JSON"}


def _a2a_shape(r):
    j = r.json()
    return r.status_code == 400 and j["jsonrpc"] == "2.0" and j["error"]["code"] == -32700


def _airgap_shape(r):
    return r.status_code == 400 and r.json() == {"error": 'body must be {"air_gapped": true|false}'}


def _rag_shape(r):
    return r.status_code == 400 and r.json() == {"error": "JSON body required"}


def _fetch_shape(r):
    return r.status_code == 422 and r.json()["error"].startswith("malformed JSON")


GUARDED = {
    "/v1/messages/count_tokens": _count_tokens_shape,
    "/v1/messages": _messages_shape,
    "/a2a/v1/": _a2a_shape,
    "/airgap": _airgap_shape,
    "/rag": _rag_shape,
    "/fetch": _fetch_shape,
}


@pytest.mark.parametrize("kind", sorted(ALL_BAD))
@pytest.mark.parametrize("path", sorted(GUARDED))
def test_j2_guarded_handlers_keep_their_shape_for_every_bad_body(client, path, kind):
    if path == "/fetch" and main._traf_extract is None:
        pytest.skip("trafilatura not installed: /fetch answers 501 before parsing")
    before = main._AIR_GAPPED
    r = _post(client, path, ALL_BAD[kind])
    assert GUARDED[path](r), (path, kind, r.status_code, r.text[:200])
    assert main._AIR_GAPPED == before  # a refused body never flips air-gap state


# ---------------------------------------------------------------------------
# J3 -- the log line and the response never carry the body
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))


@pytest.fixture
def router_log():
    lg = logging.getLogger("bifrost.router")
    h = _ListHandler()
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield h
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


def test_j3_warning_names_the_path_and_not_the_body(client, router_log):
    marker = "MARKER-must-not-be-echoed-7731"
    r = _post(client, "/v1/chat/completions", ('{"note": "%s", oops}' % marker).encode())
    assert r.status_code == 400
    assert marker not in r.text
    hits = [m for lv, m in router_log.lines if "BAD_REQUEST_JSON" in m]
    assert len(hits) == 1, router_log.lines
    lv = [lv for lv, m in router_log.lines if "BAD_REQUEST_JSON" in m][0]
    assert lv == logging.WARNING
    assert "/v1/chat/completions" in hits[0] and "invalid_json" in hits[0]
    assert all(marker not in m for _, m in router_log.lines)


def test_j3_large_body_is_not_echoed(client):
    r = _post(client, "/classify", b'{"prompt": "' + b"x" * 200000)
    assert r.status_code == 400
    assert len(r.content) < 400


# ---------------------------------------------------------------------------
# J4 -- SCOPE: a JSONDecodeError while HANDLING stays 5xx / existing handling
# ---------------------------------------------------------------------------

def _backend_decode_error(*_a, **_k):
    raise json.JSONDecodeError("Expecting value", "<html>502 Bad Gateway</html>", 0)


def test_j4_decode_error_inside_classify_stays_500(client, monkeypatch):
    monkeypatch.setattr(main, "classify", _backend_decode_error)
    r = client.post("/classify", json={"prompt": "hello"})
    assert r.status_code == 500
    assert "invalid_json" not in r.text


def test_j4_decode_error_inside_chat_completions_stays_500(client, monkeypatch):
    monkeypatch.setattr(main, "extract_user_prompt", _backend_decode_error)
    r = client.post("/v1/chat/completions",
                    json={"model": "bifrost-auto", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 500
    assert "invalid_json" not in r.text


def test_j4_decode_error_from_the_completions_backend_keeps_its_502(client, monkeypatch):
    async def _raise(*_a, **_k):
        _backend_decode_error()
    monkeypatch.setattr(main, "ollama_completion", _raise)
    r = client.post("/v1/completions", json={"prompt": "def f():"})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "bifrost_error"


def test_j4_no_app_wide_handler_for_decode_or_value_errors():
    keys = set(main.app.exception_handlers)
    assert json.JSONDecodeError not in keys
    assert ValueError not in keys
    assert Exception not in keys


def test_j4_the_400_exception_is_not_a_decode_error():
    exc = main._InvalidRequestJSON
    assert exc in main.app.exception_handlers
    assert not issubclass(exc, ValueError)
    assert not issubclass(json.JSONDecodeError, exc)


# ---------------------------------------------------------------------------
# J5 -- a valid object body still reaches the handler
# ---------------------------------------------------------------------------

def test_j5_count_tokens_valid_body_is_200(client):
    r = client.post("/v1/messages/count_tokens",
                    json={"messages": [{"role": "user", "content": "hello world"}]})
    assert r.status_code == 200
    assert isinstance(r.json()["input_tokens"], int)


def test_j5_chat_completions_valid_body_reaches_model_validation(client):
    r = client.post("/v1/chat/completions",
                    json={"model": "zz-not-an-advertised-model", "messages": []})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "model_not_found"


def test_j5_embeddings_valid_body_reaches_model_validation(client):
    r = client.post("/v1/embeddings", json={"model": "zz-not-an-embed-model", "input": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "model_not_found"
