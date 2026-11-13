"""The tier a caller DECLARES is RECORDED in Router telemetry (2026-09-11).

PENDING: coordination/pending/PENDING-router-telemetry-tier-override-record-2026-09-11.

Live evidence (Router v434, GET /telemetry/recent?n=500, 2026-09-11 ~12:40 EDT): requests sent
with X-Tier-Override: 1a-hearth were recorded as hint_override=0, classifier_source=routing_cache,
tier_history=["1a-hearth"] -- indistinguishable from a classifier pick. hint_override is the
X-Complexity-Hint flag; no field ever described X-Tier-Override.

No model or network calls: dispatch_to_tier, the routing-decision writer, the shadow
embed/record tasks and both telemetry writers are replaced by recorders; the classifier runs in
rule mode ("off"); the routing-cache serve switch is unset; enrichment is off by header; the
model field is empty (no alias pin, no /app/logs decomposition-shadow write).
"""

import json
import logging
import pathlib
import sqlite3

import pytest

from config import Tier

import main  # noqa: E402
import telemetry  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"

NEW_FIELDS = ("tier_declared", "tier_declared_source", "tier_declared_honoured",
              "tier_declared_outcome", "fanout_run_id", "fanout_leg")

OK_BODY = {
    "id": "t", "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


@pytest.fixture
def rig(monkeypatch):
    testclient = pytest.importorskip("fastapi.testclient")
    import classifier

    monkeypatch.setattr(classifier, "NPU_ROUTING_ENABLED", "off")
    monkeypatch.setattr(main, "NPU_ROUTING_ENABLED", "off", raising=False)
    monkeypatch.delenv("BIFROST_ROUTING_CACHE_SERVE", raising=False)
    monkeypatch.setattr(main.settings, "bifrost_api_key_inference", "", raising=False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(main, "_shadow_record", _noop)
    monkeypatch.setattr(main, "_embed_shadow", _noop)
    monkeypatch.setattr(main, "write_routing_decision", _noop)
    # the cloud circuit breaker is process-global: keep these tests from tripping it for others
    _cb = main.egress_guard.CLOUD_BREAKER
    monkeypatch.setattr(_cb, "allow_cloud", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(_cb, "record_failure", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_cb, "record_success", lambda *a, **k: None, raising=False)

    st = {"success": [], "failure": [], "dispatched": [], "fail_tiers": set(), "fail_all": False}

    def _emit(request_id, tokens_out, total_latency_ms, inference_event=None, task_set=None):
        st["success"].append(inference_event)

    def _write(ev):  # records synchronously; the coroutine it returns does nothing
        st["failure"].append(ev)
        return _noop()

    monkeypatch.setattr(main, "emit_completion_telemetry_fire_and_forget", _emit)
    monkeypatch.setattr(main, "_write_event", _write)

    async def _dispatch(tier, messages, stream, temperature, max_tokens,
                        caller_thinking=False, tools=None, tool_choice=None):
        st["dispatched"].append(tier.value)
        if st["fail_all"] or tier.value in st["fail_tiers"]:
            raise RuntimeError("backend down (test double)")
        if stream:
            async def _gen():
                yield "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}) + "\n\n"
                yield "data: " + json.dumps({"choices": [{"delta": {}}],
                                             "usage": OK_BODY["usage"]}) + "\n\n"
                yield "data: [DONE]\n\n"
            return _gen()
        return json.loads(json.dumps(OK_BODY))

    monkeypatch.setattr(main, "dispatch_to_tier", _dispatch)
    client = testclient.TestClient(main.app, raise_server_exceptions=True)

    def post(headers=None, model="", stream=False, tools=None):
        body = {"model": model, "messages": [{"role": "user", "content": "hello there"}],
                "stream": stream}
        if tools is not None:
            body["tools"] = tools
        h = {"X-Bifrost-Enrichment": "off"}
        h.update(headers or {})
        r = client.post("/v1/chat/completions", json=body, headers=h)
        if stream:
            _ = r.text  # drain the stream so its finally-block telemetry runs
        return r

    st["post"] = post
    return st


def _one(events):
    assert len(events) == 1, f"expected exactly one event, got {len(events)}"
    return events[0]


TOOLS = [{"type": "function", "function": {"name": "f", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]


# ---------------------------------------------------------------------------
# D1 -- a declared request is recorded as DECLARED, not as a classifier pick
# ---------------------------------------------------------------------------

def test_d1_header_override_recorded_and_honoured(rig):
    r = rig["post"]({"X-Tier-Override": "1b"})
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert rig["dispatched"] == ["1b"]
    assert ev.tier_used == "1b" and ev.tier_history == ["1b"]
    assert ev.tier_declared == "1b"
    assert ev.tier_declared_source == "header"
    assert ev.tier_declared_honoured == 1
    assert ev.tier_declared_outcome == "honored"
    # classifier_source still says what the classifier did (it DOES run on an override);
    # the declaration is what now tells the two apart.
    assert ev.classifier_source == "rule" and ev.hint_override == 0
    assert rig["failure"] == []


def test_d1_alias_pin_is_a_declaration(rig):
    r = rig["post"](model="bifrost-granite")
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert rig["dispatched"] == ["1b"]
    assert (ev.tier_declared, ev.tier_declared_source, ev.tier_declared_honoured) == ("1b", "alias_pin", 1)


def test_d1_header_beats_alias_pin(rig):
    r = rig["post"]({"X-Tier-Override": "1a-hearth"}, model="bifrost-granite")
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert rig["dispatched"] == ["1a-hearth"]
    assert (ev.tier_declared, ev.tier_declared_source, ev.tier_declared_outcome) == ("1a-hearth", "header", "honored")


def test_d1_streaming_path_records_the_declaration(rig):
    r = rig["post"]({"X-Tier-Override": "1a-hearth"}, stream=True)
    assert r.status_code == 200
    ev = _one(rig["success"])
    assert ev.tier_used == "1a-hearth"
    assert (ev.tier_declared, ev.tier_declared_honoured, ev.tier_declared_outcome) == ("1a-hearth", 1, "honored")


# ---------------------------------------------------------------------------
# D2 -- the record SURVIVES a reroute + escalation, and says it was NOT honored
# ---------------------------------------------------------------------------

def test_d2_reroute_then_escalation_keeps_declaration_honoured_false(rig, monkeypatch):
    cap = {"1a-coder": False, "2": True, "1b": True}
    monkeypatch.setattr(main, "_tier_tool_capability", lambda t: cap.get(t.value))
    monkeypatch.setattr(main, "get_cascade", lambda band: [Tier("2"), Tier("1b")])
    rig["fail_tiers"].add("2")
    r = rig["post"]({"X-Tier-Override": "1a-coder"}, tools=TOOLS)
    assert r.status_code == 200, r.text[:300]
    assert rig["dispatched"] == ["2", "1b"]          # rerouted off 1a-coder, then escalated
    ev = _one(rig["success"])
    assert ev.tier_used == "1b" and ev.tier_history == ["2", "1b"] and ev.escalation_count == 1
    assert ev.tier_declared == "1a-coder"
    assert ev.tier_declared_source == "header"
    assert ev.tier_declared_honoured == 0
    assert ev.tier_declared_outcome == "rerouted"


def test_d2_declared_tier_that_fails_leaves_a_row(rig):
    rig["fail_tiers"].add("1b")
    r = rig["post"]({"X-Tier-Override": "1b", "X-Fanout-Run-Id": "run-1", "X-Fanout-Leg": "bifrost"})
    assert r.status_code == 502
    assert rig["success"] == []
    ev = _one(rig["failure"])
    assert ev.status == "ERROR" and ev.tier_used == "" and ev.tier_history == ["1b"]
    assert ev.tokens_total == 0
    assert (ev.tier_declared, ev.tier_declared_honoured, ev.tier_declared_outcome) == ("1b", 0, "failed")
    assert (ev.fanout_run_id, ev.fanout_leg) == ("run-1", "bifrost")


def test_d2_no_tool_capable_tier_refusal_is_recorded(rig, monkeypatch):
    monkeypatch.setattr(main, "_tier_tool_capability", lambda t: False)
    r = rig["post"]({"X-Tier-Override": "1a-coder"}, tools=TOOLS)
    assert r.status_code == 503
    ev = _one(rig["failure"])
    assert (ev.status, ev.tier_used, ev.tier_declared_outcome) == ("REFUSED", "", "refused_no_tool_tier")
    assert rig["dispatched"] == []


def test_d2_ctx_overflow_refusal_is_recorded(rig, monkeypatch):
    def _reject(*a, **k):
        return {"action": "reject", "warn": False, "msg": "", "header": None, "pct": 120,
                "body": {"error": {"code": "context_overflow"}}}
    monkeypatch.setattr(main.context_overflow, "check_context", _reject)
    r = rig["post"]({"X-Tier-Override": "1b"})
    assert r.status_code == 413
    ev = _one(rig["failure"])
    assert (ev.tier_declared, ev.tier_declared_outcome, ev.tier_history) == ("1b", "refused_ctx_overflow", ["1b"])


def test_d2_air_gap_refusal_is_recorded(rig, monkeypatch):
    monkeypatch.setattr(main, "_air_gapped", lambda: True)
    monkeypatch.setattr(main, "_tier_escalation_only", lambda v: True)
    r = rig["post"]({"X-Tier-Override": "3-lite-free"})
    assert r.status_code == 503
    ev = _one(rig["failure"])
    assert (ev.tier_declared, ev.tier_used, ev.tier_declared_outcome) == ("3-lite-free", "", "refused_air_gapped")


# ---------------------------------------------------------------------------
# D3 -- refused and ignored declarations are recorded too
# ---------------------------------------------------------------------------

def test_d3_retired_refusal_is_recorded(rig):
    r = rig["post"]({"X-Tier-Override": "npu-tools"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "tier_retired"
    assert rig["dispatched"] == [] and rig["success"] == []
    ev = _one(rig["failure"])
    assert (ev.status, ev.tier_used) == ("REFUSED", "")
    assert (ev.tier_declared, ev.tier_declared_source) == ("npu-tools", "header")
    assert (ev.tier_declared_honoured, ev.tier_declared_outcome) == (0, "refused_retired")


def test_d3_unknown_tier_ignored_is_recorded(rig):
    r = rig["post"]({"X-Tier-Override": "zz-not-a-tier"})
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert (ev.tier_declared, ev.tier_declared_honoured, ev.tier_declared_outcome) == ("zz-not-a-tier", 0, "unknown_ignored")


def test_d3_failure_record_never_changes_the_response(rig, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("telemetry broke")
    monkeypatch.setattr(main, "declared_failure_event", _boom)
    r = rig["post"]({"X-Tier-Override": "npu-tools"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "tier_retired"


# ---------------------------------------------------------------------------
# D4 -- an UNDECLARED request keeps today's fields, and gains no rows
# ---------------------------------------------------------------------------

def test_d4_undeclared_request_unchanged(rig):
    r = rig["post"]()
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert ev.tier_used == rig["dispatched"][0]
    assert ev.tier_history == [rig["dispatched"][0]]
    assert ev.classifier_source == "rule" and ev.hint_override == 0
    assert all(getattr(ev, f) is None for f in NEW_FIELDS[:4])
    assert (ev.fanout_run_id, ev.fanout_leg) == ("", "")
    assert rig["failure"] == []


def test_d4_undeclared_failure_writes_nothing_new(rig):
    rig["fail_all"] = True
    r = rig["post"]()
    assert r.status_code == 502
    assert rig["success"] == [] and rig["failure"] == []


# ---------------------------------------------------------------------------
# D5 -- fan-out identity
# ---------------------------------------------------------------------------

def test_d5_fanout_ids_recorded_and_graph_fields_untouched(rig):
    r = rig["post"]({"X-Tier-Override": "2", "X-Fanout-Run-Id": "fan-20260911-1a2b",
                     "X-Fanout-Leg": "forge"})
    assert r.status_code == 200, r.text[:300]
    ev = _one(rig["success"])
    assert (ev.fanout_run_id, ev.fanout_leg) == ("fan-20260911-1a2b", "forge")
    assert (ev.graph_id, ev.subtask_id) == ("", "")   # AUTOPILOT join key is not reused
    assert (ev.tier_declared, ev.tier_declared_outcome) == ("2", "honored")


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "leg;drop", "-leading", "\u00e9t\u00e9"])
def test_d5_unsafe_ids_are_dropped(bad):
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k.lower(), d)
    d = telemetry.read_tier_declaration(H({"x-fanout-run-id": bad, "x-fanout-leg": bad}),
                                        None, frozenset(), Tier)
    assert (d.fanout_run_id, d.fanout_leg) == ("", "")


# ---------------------------------------------------------------------------
# D6 -- read_tier_declaration MIRRORS main.py's override block (executed verbatim)
# ---------------------------------------------------------------------------

HEAD = '    _tier_override_str = request.headers.get("X-Tier-Override", "").strip()'
TAIL = ("            log.warning(f\"TIER_OVERRIDE: unknown tier '{_tier_override_str}', "
        "ignoring override\")")


class _Headers:
    def __init__(self, mapping):
        self._m = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._m.get(str(key).lower(), default)


class _Request:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def _run_block(header_value, pin):
    src = MAIN.read_text(encoding="utf-8")
    assert src.count(HEAD) == 1 and src.count(TAIL) == 1
    body = src[src.index(HEAD): src.index(TAIL) + len(TAIL)]
    code = "def _probe(request, _model_alias_tier):\n" + body + "\n    return _tier_override\n"
    ns = {"Tier": Tier, "JSONResponse": main.JSONResponse,
          "log": logging.getLogger("test_tier_declaration_record"),
          "_RETIRED_TIER_OVERRIDES": main._RETIRED_TIER_OVERRIDES}
    exec(compile(code, str(MAIN), "exec"), ns)
    headers = {} if header_value is None else {"X-Tier-Override": header_value}
    return ns["_probe"](_Request(headers), pin)


@pytest.mark.parametrize("value", [None, "", "1b", "  1b ", "npu", "2.5-solo", "3-lite-free",
                                   "zz-not-a-tier", "NPU-TOOLS", "npu-tools", "1a-coder-tool"])
@pytest.mark.parametrize("pin", [None, "2"])
def test_d6_declaration_mirrors_the_override_block(value, pin):
    pin_t = Tier(pin) if pin else None
    got = _run_block(value, pin_t)
    headers = _Headers({} if value is None else {"X-Tier-Override": value})
    decl = telemetry.read_tier_declaration(headers, pin_t, main._RETIRED_TIER_OVERRIDES, Tier)
    if isinstance(got, main.JSONResponse):
        assert got.status_code == 400 and decl.status == "refused_retired"
    elif got is None:
        assert decl.tier is None or decl.status == "unknown_ignored"
    else:
        assert decl.status == "declared" and decl.tier == got.value


# ---------------------------------------------------------------------------
# D7 -- storage: round trip, column order, and the downstream readers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "TELEMETRY_DIR", tmp_path)
    monkeypatch.setattr(telemetry, "TELEMETRY_DB", tmp_path / "inference_events.db")
    return tmp_path / "inference_events.db"


def _decl(tier="3-lite-free", status="declared", run="r1", leg="cloud"):
    return telemetry.TierDeclaration(tier, "header", status, run, leg)


def test_d7_round_trip_through_query_recent(tmpdb):
    d = _decl("1b", run="r1", leg="bifrost")
    telemetry.write_event_sync(telemetry.InferenceEvent(tier_used="1b", tier_history=["1b"],
                                                        tokens_total=7, **d.fields("1b")))
    row = telemetry.query_recent(1)[0]
    assert row["tier_declared"] == "1b" and row["tier_declared_source"] == "header"
    assert row["tier_declared_honoured"] == 1 and row["tier_declared_outcome"] == "honored"
    assert (row["fanout_run_id"], row["fanout_leg"]) == ("r1", "bifrost")
    assert row["tokens_total"] == 7


def test_d7_fresh_and_upgraded_dbs_have_the_same_column_order(tmp_path):
    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    fresh.execute(telemetry._CREATE_SQL)
    telemetry._ensure_inference_events_columns(fresh)
    fresh_cols = [r[1] for r in fresh.execute("PRAGMA table_info(inference_events)")]
    # an OLD DB: the CREATE statement without the six new columns, then the ALTER upgrade
    old_sql = "\n".join(ln for ln in telemetry._CREATE_SQL.split("\n")
                        if not any(ln.strip().startswith(f + " ") for f in NEW_FIELDS))
    old_sql = old_sql.replace("verify_level          INTEGER,", "verify_level          INTEGER")
    old = sqlite3.connect(str(tmp_path / "old.db"))
    old.execute(old_sql)
    before = [r[1] for r in old.execute("PRAGMA table_info(inference_events)")]
    assert not set(NEW_FIELDS) & set(before)
    telemetry._ensure_inference_events_columns(old)
    old_cols = [r[1] for r in old.execute("PRAGMA table_info(inference_events)")]
    assert old_cols == fresh_cols
    assert fresh_cols[-6:] == list(NEW_FIELDS)


def test_d7_failure_rows_do_not_touch_token_sums_or_the_free_tier_budget(tmpdb):
    from free_tier_budget import FreeTierBudget
    d = _decl("3-lite-free")
    telemetry.write_event_sync(telemetry.declared_failure_event(
        d, status="ERROR", outcome="failed", tried=["3-lite-free"], band="COMPLEX"))
    con = sqlite3.connect(str(tmpdb))
    assert con.execute("SELECT COALESCE(SUM(tokens_total),0) FROM inference_events").fetchone()[0] == 0
    budget = FreeTierBudget(db_path=str(tmpdb))
    assert budget._today_from_db() == 0      # a failed declared cloud leg spends no budget
    telemetry.write_event_sync(telemetry.InferenceEvent(tier_used="3-lite-free", tokens_total=9,
                                                        **d.fields("3-lite-free")))
    assert budget._today_from_db() == 1      # a served one still counts, as before
    assert con.execute("SELECT SUM(tokens_total) FROM inference_events").fetchone()[0] == 9


def test_d7_telemetry_recent_endpoint_serves_the_new_keys(tmpdb):
    testclient = pytest.importorskip("fastapi.testclient")
    telemetry.write_event_sync(telemetry.InferenceEvent(tier_used="2", tokens_total=4,
                                                        **_decl("2").fields("2")))
    r = testclient.TestClient(main.app).get("/telemetry/recent?n=5")
    ev = r.json()["events"][0]
    assert ev["tier_used"] == "2" and ev["tokens_total"] == 4
    assert ev["tier_declared"] == "2" and ev["tier_declared_outcome"] == "honored"
