"""Tiers 1a-coder-tool and npu-tools are RETIRED (2026-09-11).

PENDING: coordination/pending/PENDING-router-retire-tiers-1a-coder-tool-and-npu-tools-2026-09-11.
M2b removed their aliases; this change removes the tiers themselves: the config.Tier members,
the fleet_config.json entries and main._TOOL_LOOP_BUDGETS' npu-tools row.

The load-bearing group is the last one. Before this change an X-Tier-Override naming a value
that is not a Tier member was IGNORED and the request took the normal cascade. For a retired
tier that is the wrong failure: a caller who asked for a local leaf could be served by a cloud
tier without being told. It must now fail cleanly: HTTP 400, code tier_retired, before any
backend is touched -- and any OTHER unknown value keeps the old ignore behavior.

The override tests execute main.py's literal header block (house pattern, see
test_pipeline_optin.py), so they cannot drift from the protected file.
"""

import json
import logging
import pathlib

import pytest

from config import Tier

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"
RETIRED = ("1a-coder-tool", "npu-tools")


def _fleet_tiers():
    with open(REPO / "fleet_config.json", encoding="utf-8") as fh:
        return [t["tier"] for t in json.load(fh)["tiers"]]


# ---------------------------------------------------------------------------
# R1 -- the tiers are gone from the config of record
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", RETIRED)
def test_r1_enum_has_no_retired_member(value):
    assert value not in {t.value for t in Tier}
    with pytest.raises(ValueError):
        Tier(value)


def test_r1_enum_member_names_gone():
    assert not hasattr(Tier, "T1A_CODER_TOOL")
    assert not hasattr(Tier, "T_NPU_TOOLS")


@pytest.mark.parametrize("value", RETIRED)
def test_r1_fleet_config_has_no_retired_tier(value):
    assert value not in _fleet_tiers()


def test_r1_npu_classifier_tier_stays():
    """T_NPU (the classifier tier, alias bifrost-npu) is NOT retired -- only npu-tools is."""
    assert Tier.T_NPU.value == "npu"
    assert "npu" in _fleet_tiers()


def test_r1_fleet_tiers_still_subset_of_enum():
    """startup_checks drift / preflight tier-enum-subset: every fleet tier has an enum member."""
    enum_values = {t.value for t in Tier}
    assert [t for t in _fleet_tiers() if t not in enum_values] == []


# ---------------------------------------------------------------------------
# R2 -- main.py carries no live reference
# ---------------------------------------------------------------------------

import main  # noqa: E402  (heavy import after the light checks)


def test_r2_tool_loop_budgets_has_no_retired_row():
    assert all(getattr(k, "value", k) not in RETIRED for k in main._TOOL_LOOP_BUDGETS)
    # the reader's fallback still works for every live tier
    for t in Tier:
        iters, timeout = main._TOOL_LOOP_BUDGETS.get(t, main._TOOL_LOOP_BUDGET_DEFAULT)
        assert iters > 0 and timeout > 0


def test_r2_retired_set_is_exactly_the_two_and_disjoint_from_live_tiers():
    assert main._RETIRED_TIER_OVERRIDES == frozenset(RETIRED)
    assert not (main._RETIRED_TIER_OVERRIDES & {t.value for t in Tier})


def test_r2_no_alias_pins_a_retired_tier():
    assert all(t.value not in RETIRED for t in main._MODEL_ALIAS_TIER_PINS.values())


# ---------------------------------------------------------------------------
# R3 -- X-Tier-Override naming a retired tier FAILS CLEANLY (the load-bearing group)
# ---------------------------------------------------------------------------

HEAD = '    _tier_override_str = request.headers.get("X-Tier-Override", "").strip()'
TAIL = ("            log.warning(f\"TIER_OVERRIDE: unknown tier '{_tier_override_str}', "
        "ignoring override\")")


class _Headers:
    """Minimal stand-in for starlette Headers: case-insensitive .get(), like the real one."""

    def __init__(self, mapping):
        self._m = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._m.get(str(key).lower(), default)


class _Request:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def _block_source():
    """main.py's verbatim header block, wrapped as a function body (it contains a return)."""
    src = MAIN.read_text(encoding="utf-8")  # universal newlines: CRLF -> \n
    assert src.count(HEAD) == 1, "override head must appear exactly once in main.py"
    assert src.count(TAIL) == 1, "override tail must appear exactly once in main.py"
    i = src.index(HEAD)
    j = src.index(TAIL) + len(TAIL)
    return ("def _probe(request, _model_alias_tier):\n" + src[i:j]
            + "\n    return _tier_override\n")


def run_block(header_value, pin=None, retired=None):
    ns = {
        "Tier": Tier,
        "JSONResponse": main.JSONResponse,
        "log": logging.getLogger("test_retired_tiers"),
        "_RETIRED_TIER_OVERRIDES": main._RETIRED_TIER_OVERRIDES if retired is None else retired,
    }
    exec(compile(_block_source(), str(MAIN), "exec"), ns)
    headers = {} if header_value is None else {"X-Tier-Override": header_value}
    return ns["_probe"](_Request(headers), pin)


@pytest.mark.parametrize("value", RETIRED)
def test_r3_retired_override_returns_400_tier_retired(value):
    resp = run_block(value)
    assert isinstance(resp, main.JSONResponse), "a retired tier must short-circuit with a response"
    assert resp.status_code == 400
    err = json.loads(resp.body)["error"]
    assert err["code"] == "tier_retired"
    assert err["type"] == "invalid_request_error"
    assert value in err["message"]


@pytest.mark.parametrize("value", RETIRED)
def test_r3_retired_override_wins_over_a_model_alias_pin(value):
    resp = run_block(value, pin=Tier("1b"))
    assert isinstance(resp, main.JSONResponse) and resp.status_code == 400


@pytest.mark.parametrize("value", ["1b", "npu", "2.5-solo"])
def test_r3_live_tier_override_still_honoured(value):
    assert run_block(value) is Tier(value)


@pytest.mark.parametrize("value", ["zz-not-a-tier", "NPU-TOOLS", "1a-coder-toolx"])
def test_r3_other_unknown_values_keep_the_ignore_behaviour(value):
    pin = Tier("2")
    assert run_block(value, pin=pin) is pin
    assert run_block(value) is None


def test_r3_no_header_is_unchanged():
    pin = Tier("2")
    assert run_block(None, pin=pin) is pin
    assert run_block("", pin=pin) is pin


@pytest.mark.parametrize("value", RETIRED)
def test_r3_negative_control_without_the_guard_the_header_is_silently_ignored(value):
    """Proves the guard is what makes the difference: with the set emptied, the retired value
    falls into the generic ValueError branch and the request would route normally."""
    pin = Tier("2")
    assert run_block(value, pin=pin, retired=frozenset()) is pin


@pytest.mark.parametrize("value", RETIRED)
def test_r3_http_end_to_end_is_a_clean_400_not_a_500(value, monkeypatch):
    """Through the real ASGI app. The guard returns before classification or dispatch, so no
    backend is contacted (the lifespan does not run: TestClient is not used as a context)."""
    testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)
    assert "bifrost-auto" in main._ADVERTISED_MODEL_IDS, "precondition: an advertised model"
    client = testclient.TestClient(main.app, raise_server_exceptions=True)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "bifrost-auto", "messages": [{"role": "user", "content": "hello"}]},
        headers={"X-Tier-Override": value},
    )
    assert r.status_code == 400, r.text[:300]
    err = r.json()["error"]
    assert err["code"] == "tier_retired"
    assert value in err["message"]
