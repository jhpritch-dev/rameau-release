"""SEC-2 scope checks on /fleet/load (admin) and /fleet/config (ops) are ENFORCED, not discarded.

PENDING: coordination/pending/PENDING-router-admin-endpoint-auth-2026-09-11.
Both handlers called _validate_api_key(...) as a bare statement. Its 401 JSONResponse was thrown
away, so each handler ran for any caller even when BIFROST_API_KEY_ADMIN / _OPS were configured.
They now return it, exactly as /v1/chat/completions and /autopilot/submit do.

Groups:
  A1  /fleet/load with scoped keys configured: no key, a wrong key, the ops key or the inference
      key -> 401 SEC-2 scope=admin, BEFORE the body is read, with no side effect: fleet config
      never read, no Ollama client made, no fleet_config.json written.
  A2  /fleet/load with the admin key proceeds: one /api/generate call, active_model written.
  A3  /fleet/config (ops scope): no key / wrong key / inference key -> 401 scope=ops and the
      config is never read; the ops key and the admin key both read it.
  A4  open mode is UNCHANGED: with no key configured for a scope, both endpoints still answer a
      caller with no key -- the same pass-through _validate_api_key gives every caller today.
  A5  a refusal never echoes the supplied key, in the response or in any log line.
  A6  source scan: no _validate_api_key(...) call in main.py discards its result.

No backend is contacted: httpx.AsyncClient is replaced by a recorder, config._load_fleet_config
is replaced and config._FLEET_CONFIG_PATH points into tmp_path. TestClient is not used as a
context manager, so the lifespan (poll tasks, http client) never runs.
"""

import ast
import copy
import json
import logging
from pathlib import Path

import pytest

testclient = pytest.importorskip("fastapi.testclient")

import config  # noqa: E402
import main  # noqa: E402

ADMIN_KEY = "test-admin-key-1111"
OPS_KEY = "test-ops-key-2222"
INFER_KEY = "test-inference-key-3333"
WRONG_KEY = "test-wrong-key-9999"
SCOPED = {INFER_KEY: "inference", OPS_KEY: "ops", ADMIN_KEY: "admin"}

TIER = "zz-test-tier"
FAKE_BASE = "http://fake-ollama.invalid:11434"
FLEET = {"tiers": [{"tier": TIER, "type": "ollama", "base_url": FAKE_BASE,
                    "active_model": "old-model", "shared_instance_with": ["zz-peer"]}]}
LOAD_BODY = {"tier": TIER, "model": "new-model"}
SEC2_ADMIN = {"error": "Invalid or missing API key", "code": "SEC-2", "scope": "admin"}
SEC2_OPS = {"error": "Invalid or missing API key", "code": "SEC-2", "scope": "ops"}


class _Recorder:
    def __init__(self, fleet_path):
        self.fleet_path = fleet_path
        self.loads = 0      # config._load_fleet_config() calls
        self.clients = 0    # httpx.AsyncClient instantiations
        self.posts = []     # (url, json) sent to the fake Ollama


@pytest.fixture
def rec(monkeypatch, tmp_path):
    r = _Recorder(tmp_path / "fleet_config.json")

    def _fake_load():
        r.loads += 1
        return copy.deepcopy(FLEET)

    class _Resp:
        status_code = 200

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            r.clients += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **k):
            r.posts.append((url, json))
            return _Resp()

    # The handlers do `from config import ...` at call time, so patching the module is enough.
    monkeypatch.setattr(config, "_load_fleet_config", _fake_load)
    monkeypatch.setattr(config, "_FLEET_CONFIG_PATH", r.fleet_path)
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    return r


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(main, "_SCOPED_KEYS", dict(SCOPED))


@pytest.fixture
def open_mode(monkeypatch):
    monkeypatch.setattr(main, "_SCOPED_KEYS", {})


@pytest.fixture
def client():
    return testclient.TestClient(main.app, raise_server_exceptions=False)


def _hdr(key):
    return {"Authorization": f"Bearer {key}"} if key else {}


def _assert_no_side_effect(rec):
    assert rec.posts == [], rec.posts
    assert rec.clients == 0
    assert rec.loads == 0
    assert not rec.fleet_path.exists()


# ---------------------------------------------------------------------------
# A1 -- /fleet/load refused without the admin key, with no side effect
# ---------------------------------------------------------------------------

REFUSED_FOR_ADMIN = {"no_key": None, "wrong_key": WRONG_KEY, "ops_key": OPS_KEY, "inference_key": INFER_KEY}


@pytest.mark.parametrize("who", sorted(REFUSED_FOR_ADMIN))
def test_a1_fleet_load_refused_without_the_admin_key(client, keyed, rec, who):
    r = client.post("/fleet/load", json=LOAD_BODY, headers=_hdr(REFUSED_FOR_ADMIN[who]))
    assert (r.status_code, r.json()) == (401, SEC2_ADMIN), (who, r.status_code, r.text[:200])
    _assert_no_side_effect(rec)


def test_a1_fleet_load_refusal_comes_before_the_body_is_read(client, keyed, rec):
    r = client.post("/fleet/load", content=b"{not json", headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (401, SEC2_ADMIN), (r.status_code, r.text[:200])
    _assert_no_side_effect(rec)


# ---------------------------------------------------------------------------
# A2 -- /fleet/load with the admin key proceeds
# ---------------------------------------------------------------------------

def test_a2_fleet_load_with_the_admin_key_proceeds(client, keyed, rec):
    r = client.post("/fleet/load", json=LOAD_BODY, headers=_hdr(ADMIN_KEY))
    assert r.status_code == 200, r.text[:200]
    assert r.json() == {"tier": TIER, "model": "new-model", "status": "loaded", "co_invalidated": ["zz-peer"]}
    assert rec.clients == 1
    assert rec.posts == [(FAKE_BASE + "/api/generate",
                          {"model": "new-model", "keep_alive": "24h", "prompt": ""})]
    written = json.loads(rec.fleet_path.read_text(encoding="utf-8"))
    assert written["tiers"][0]["active_model"] == "new-model"


# ---------------------------------------------------------------------------
# A3 -- /fleet/config (ops scope)
# ---------------------------------------------------------------------------

REFUSED_FOR_OPS = {"no_key": None, "wrong_key": WRONG_KEY, "inference_key": INFER_KEY}


@pytest.mark.parametrize("who", sorted(REFUSED_FOR_OPS))
def test_a3_fleet_config_refused_without_an_ops_key(client, keyed, rec, who):
    r = client.get("/fleet/config", headers=_hdr(REFUSED_FOR_OPS[who]))
    assert (r.status_code, r.json()) == (401, SEC2_OPS), (who, r.status_code, r.text[:200])
    assert rec.loads == 0
    assert TIER not in r.text


@pytest.mark.parametrize("who", ["ops_key", "admin_key"])
def test_a3_fleet_config_ops_or_admin_key_reads_it(client, keyed, rec, who):
    r = client.get("/fleet/config", headers=_hdr(OPS_KEY if who == "ops_key" else ADMIN_KEY))
    assert (r.status_code, r.json()) == (200, FLEET), (who, r.status_code, r.text[:200])
    assert rec.loads >= 1


# ---------------------------------------------------------------------------
# A4 -- open mode is unchanged (these pass before AND after, on purpose)
# ---------------------------------------------------------------------------

def test_a4_open_mode_fleet_load_still_answers_without_a_key(client, open_mode, rec):
    r = client.post("/fleet/load", json=LOAD_BODY)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["status"] == "loaded"
    assert len(rec.posts) == 1


def test_a4_open_mode_fleet_config_still_answers_without_a_key(client, open_mode, rec):
    r = client.get("/fleet/config")
    assert (r.status_code, r.json()) == (200, FLEET)


def test_a4_admin_scope_with_no_admin_key_configured_passes_through(client, monkeypatch, rec):
    # Existing _validate_api_key rule, NOT changed here: a scope with no key configured for it
    # passes through, so ops + inference keys alone leave /fleet/load open. See the PENDING's
    # open questions.
    monkeypatch.setattr(main, "_SCOPED_KEYS", {OPS_KEY: "ops", INFER_KEY: "inference"})
    r = client.post("/fleet/load", json=LOAD_BODY)
    assert r.status_code == 200, r.text[:200]


# ---------------------------------------------------------------------------
# A5 -- the refusal never echoes the key
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


@pytest.mark.parametrize("path,method", [("/fleet/load", "post"), ("/fleet/config", "get")])
def test_a5_refusal_never_echoes_the_key(client, keyed, rec, router_log, path, method):
    kw = {"json": LOAD_BODY} if method == "post" else {}
    r = getattr(client, method)(path, headers=_hdr(WRONG_KEY), **kw)
    assert r.status_code == 401, (path, r.status_code, r.text[:200])
    assert WRONG_KEY not in r.text
    assert any("SEC-2: 401 scope=" in m for _, m in router_log.lines), router_log.lines
    assert all(WRONG_KEY not in m for _, m in router_log.lines)


# ---------------------------------------------------------------------------
# A6 -- no call site discards the result
# ---------------------------------------------------------------------------

def _validate_calls():
    """(discarded line numbers, guarded count) for _validate_api_key calls in main.py."""
    tree = ast.parse(Path(main.__file__).read_bytes())
    discarded, guarded, seen = [], 0, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_validate_api_key":
            seen += 1
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, s in enumerate(stmts):
                is_call = lambda v: isinstance(v, ast.Call) and getattr(v.func, "id", None) == "_validate_api_key"
                if isinstance(s, ast.Expr) and is_call(s.value):
                    discarded.append(s.lineno)
                elif isinstance(s, ast.Assign) and is_call(s.value) and isinstance(s.targets[0], ast.Name):
                    tgt = s.targets[0].id
                    nxt = stmts[i + 1] if i + 1 < len(stmts) else None
                    if (isinstance(nxt, ast.If) and isinstance(nxt.test, ast.Name) and nxt.test.id == tgt
                            and isinstance(nxt.body[0], ast.Return)
                            and isinstance(nxt.body[0].value, ast.Name) and nxt.body[0].value.id == tgt):
                        guarded += 1
    return discarded, guarded, seen


def test_a6_no_validate_api_key_result_is_discarded():
    discarded, guarded, seen = _validate_calls()
    assert seen >= 4, seen  # the scan must actually find the call sites, or it proves nothing
    assert discarded == [], f"_validate_api_key result discarded at main.py line(s) {discarded}"
    assert guarded == seen, (guarded, seen)
