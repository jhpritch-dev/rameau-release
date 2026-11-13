"""SEC-2 open mode is per scope: an admin key alone must not close the ops scope.

PENDING: coordination/pending/PENDING-router-admin-key-ops-open-mode-2026-09-11.
Setting only BIFROST_API_KEY_ADMIN (to protect POST /fleet/load) used to close the ops scope as well,
so the portal's keyless GET /fleet/config got 401. Now a scope is enforced only once a key for THAT
scope exists, and a configured admin key is still accepted on every scope.

_validate_api_key is called directly with a stub request; nothing is contacted.
"""

import pytest

import main

ADMIN = "t-admin-key-1111"
OPS = "t-ops-key-2222"
INFER = "t-inference-key-3333"
WRONG = "t-wrong-key-9999"


class _Req:
    def __init__(self, key=None):
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}


def _check(monkeypatch, keys, scope, key):
    monkeypatch.setattr(main, "_SCOPED_KEYS", dict(keys))
    r = main._validate_api_key(_Req(key), required_scope=scope)
    return None if r is None else r.status_code


ONLY_ADMIN = {ADMIN: "admin"}
OPS_AND_ADMIN = {OPS: "ops", ADMIN: "admin"}
ONLY_OPS = {OPS: "ops"}
ALL3 = {INFER: "inference", OPS: "ops", ADMIN: "admin"}


# The fix: an admin key alone leaves the ops scope open (the portal's /fleet/config keeps working).
@pytest.mark.parametrize("key", [None, WRONG, ADMIN])
def test_only_admin_key_leaves_ops_open(monkeypatch, key):
    assert _check(monkeypatch, ONLY_ADMIN, "ops", key) is None


# ...and still closes the admin scope (/fleet/load).
@pytest.mark.parametrize("key,want", [(None, 401), (WRONG, 401), (ADMIN, None)])
def test_only_admin_key_closes_admin(monkeypatch, key, want):
    assert _check(monkeypatch, ONLY_ADMIN, "admin", key) == want


# Once an ops key exists, ops is enforced; the ops key and the admin key both pass.
@pytest.mark.parametrize("keys", [OPS_AND_ADMIN, ONLY_OPS, ALL3])
@pytest.mark.parametrize("key,want", [(None, 401), (WRONG, 401), (INFER, 401), (OPS, None)])
def test_ops_key_configured_enforces_ops(monkeypatch, keys, key, want):
    assert _check(monkeypatch, keys, "ops", key) == want


@pytest.mark.parametrize("keys", [OPS_AND_ADMIN, ALL3])
def test_admin_key_accepted_on_ops(monkeypatch, keys):
    assert _check(monkeypatch, keys, "ops", ADMIN) is None


# Unchanged: with an ops key but no admin key, the admin scope stays open (as before this change).
def test_only_ops_key_leaves_admin_open(monkeypatch):
    assert _check(monkeypatch, ONLY_OPS, "admin", None) is None


# Unchanged: no keys at all -> every scope open.
@pytest.mark.parametrize("scope", ["ops", "admin"])
def test_no_keys_open(monkeypatch, scope):
    assert _check(monkeypatch, {}, scope, None) is None


# Unchanged: an ops key does not pass the admin scope.
def test_ops_key_rejected_on_admin(monkeypatch):
    assert _check(monkeypatch, ALL3, "admin", OPS) == 401
