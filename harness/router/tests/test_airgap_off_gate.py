"""POST /airgap: OFF needs the control token, ON stays open (John 2026-09-15, "gate OFF direction only").

PENDING: coordination/pending/PENDING-router-airgap-off-gate-2026-09-15 (diff 893DFAEC).
OFF re-admits cloud tiers, so it goes through _require_control (401 bad/missing header, 503 when no token is
configured). ON is the safe direction; the Portal's badge only ever sends air_gapped=true, with no header.
A malformed body is still refused with 400 BEFORE the gate (test_request_json_errors J2 keeps its shape).
Every case monkeypatches the three air-gap globals, so no case leaks state into another test.
"""

import pytest

testclient = pytest.importorskip("fastapi.testclient")

import main  # noqa: E402

CONTROL = "test-control-token"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)
    monkeypatch.setattr(main.settings, "bifrost_api_key_autopilot", "", raising=False)
    monkeypatch.setattr(main, "_CONTROL_TOKEN", CONTROL)
    monkeypatch.setattr(main, "_AIR_GAPPED", False)
    monkeypatch.setattr(main, "_AIR_GAPPED_SOURCE", "env")
    monkeypatch.setattr(main, "_AIR_GAPPED_AT", None)
    return testclient.TestClient(main.app, raise_server_exceptions=False)


def _state():
    return (main._AIR_GAPPED, main._AIR_GAPPED_SOURCE, main._AIR_GAPPED_AT)


def test_off_without_header_is_401_and_changes_nothing(client):
    before = _state()
    r = client.post("/airgap", json={"air_gapped": False})
    assert r.status_code == 401, r.text[:200]
    assert _state() == before


def test_off_with_wrong_header_is_401(client):
    before = _state()
    r = client.post("/airgap", json={"air_gapped": False}, headers={"X-Bifrost-Control": "wrong"})
    assert r.status_code == 401
    assert _state() == before


def test_off_with_no_token_configured_fails_closed_503(client, monkeypatch):
    monkeypatch.setattr(main, "_CONTROL_TOKEN", "")
    before = _state()
    r = client.post("/airgap", json={"air_gapped": False}, headers={"X-Bifrost-Control": CONTROL})
    assert r.status_code == 503
    assert _state() == before


def test_off_with_token_when_not_airgapped_is_200(client):
    r = client.post("/airgap", json={"air_gapped": False}, headers={"X-Bifrost-Control": CONTROL})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["current"] is False


def test_off_with_token_while_latched_on_still_409(client, monkeypatch):
    monkeypatch.setattr(main, "_AIR_GAPPED", True)
    r = client.post("/airgap", json={"air_gapped": False}, headers={"X-Bifrost-Control": CONTROL})
    assert r.status_code == 409
    assert main._AIR_GAPPED is True


def test_on_without_header_stays_open(client):
    r = client.post("/airgap", json={"air_gapped": True})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["current"] is True
    assert main._AIR_GAPPED is True


def test_malformed_body_without_header_is_still_400_before_the_gate(client):
    r = client.post("/airgap", content=b"not json", headers=JSON)
    assert r.status_code == 400
    assert r.json() == {"error": 'body must be {"air_gapped": true|false}'}


def test_get_airgap_needs_no_header(client):
    r = client.get("/airgap")
    assert r.status_code == 200
    assert r.json()["air_gapped"] is False
