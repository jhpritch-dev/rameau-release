"""/search must say whether the KB reranked, because its consumer floors and re-sorts the rows.

HUNK E, 2026-09-27. `_query_kb_async` has read the response's `reranked` flag since D-1 and then threw
it away. /search hands its rows straight to igor_tool_loop._call_search, which applies a cosine floor
and a cosine sort -- to a list the cross-encoder had already ranked and selected. Measured over 25 live
queries, that discards the reranker's rank-1 row on 16% of them. The consumer cannot pick the right
instrument without knowing the scale, so the endpoint now reports it.

The two enrichment call sites (_enrich_messages_with_rag, /v1/chat/completions) deliberately do NOT ask
for the meta, so their return shape is unchanged. That is asserted here, not assumed.
"""
import ast
import asyncio
import inspect

import pytest

import main


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


def _serve(monkeypatch, payload, status=200, explode=False):
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if explode:
                raise RuntimeError("connection refused")
            return _Resp(payload, status)

    monkeypatch.setattr(main.httpx, "AsyncClient", _C)


def _sem(sim, src):
    return {"similarity": sim, "source": src, "text": "body from " + src,
            "tier": None, "fact": None}


MEASURED = [_sem(0.568, "wp-v4_7"), _sem(0.669, "wp-v2"), _sem(0.629, "wp-v3")]


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- the two return shapes
def test_without_with_meta_the_return_shape_is_unchanged(monkeypatch):
    _serve(monkeypatch, {"chunks": MEASURED, "reranked": True})
    got = _run(main._query_kb_async("q"))
    assert isinstance(got, list)
    assert got and all(isinstance(c, dict) for c in got)


def test_with_meta_returns_rows_and_the_flag(monkeypatch):
    _serve(monkeypatch, {"chunks": MEASURED, "reranked": True})
    rows, meta = _run(main._query_kb_async("q", with_meta=True))
    assert isinstance(rows, list) and len(rows) == 3
    assert meta["reranked"] is True
    assert meta["raw"] == 3 and meta["kept"] == 3


def test_the_flag_is_the_responses_own_not_a_constant(monkeypatch):
    _serve(monkeypatch, {"chunks": MEASURED})
    rows, meta = _run(main._query_kb_async("q", with_meta=True))
    assert meta["reranked"] is False
    # and with the flag absent the floor applied, so the 0.568 row is gone
    assert [c["source"] for c in rows] == ["wp-v2", "wp-v3"]


def test_meta_counts_report_what_was_discarded(monkeypatch):
    """raw vs kept is the only place a reader can see the selector dropped anything."""
    _serve(monkeypatch, {"chunks": MEASURED})
    _, meta = _run(main._query_kb_async("q", with_meta=True))
    assert (meta["raw"], meta["kept"]) == (3, 2)


# --------------------------------------------------------------- failure paths keep their shape
@pytest.mark.parametrize("kw,shape", [({}, list), ({"with_meta": True}, tuple)])
def test_a_non_200_keeps_the_callers_shape(monkeypatch, kw, shape):
    _serve(monkeypatch, {"chunks": MEASURED}, status=503)
    got = _run(main._query_kb_async("q", **kw))
    assert isinstance(got, shape)
    if shape is tuple:
        rows, meta = got
        assert rows == [] and meta["reranked"] is False


@pytest.mark.parametrize("kw,shape", [({}, list), ({"with_meta": True}, tuple)])
def test_an_unreachable_service_keeps_the_callers_shape(monkeypatch, kw, shape):
    """A tuple-unpacking caller must not get a bare list on the error path -- that is a TypeError at
    the call site, i.e. /search would 500 whenever the KB was down instead of degrading."""
    _serve(monkeypatch, {}, explode=True)
    got = _run(main._query_kb_async("q", **kw))
    assert isinstance(got, shape)
    if shape is tuple:
        assert got[0] == [] and got[1]["reranked"] is False


def test_with_meta_is_keyword_only():
    sig = inspect.signature(main._query_kb_async)
    assert sig.parameters["with_meta"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["with_meta"].default is False


# --------------------------------------------------------------- the endpoint wiring
def _endpoint_src():
    return inspect.getsource(main.search_endpoint)


def test_search_asks_for_the_meta_and_unpacks_it():
    src = _endpoint_src()
    assert "with_meta=True" in src, src
    assert "(kb_chunks, kb_meta), web_results = await asyncio.gather" in src, src


def test_search_puts_reranked_in_the_body_and_in_a_header():
    src = _endpoint_src()
    assert '"reranked": _kb_reranked' in src, "the JSON body does not carry the flag"
    assert 'resp.headers["X-Bifrost-KB-Reranked"]' in src, "no header"
    assert "_kb_reranked = bool(kb_meta.get('reranked'))" in src, src


def test_the_flag_in_the_body_is_not_hardcoded():
    """A literal True would tell every consumer to switch its floor off, including when the service
    did not rerank -- which is the opposite failure to the one this repairs."""
    fn = ast.parse(inspect.getsource(main.search_endpoint).lstrip()).body[0]
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_kb_reranked" for t in node.targets):
            assert isinstance(node.value, ast.Call), "the flag is not computed from the response"
            assert "kb_meta" in ast.dump(node.value), ast.dump(node.value)
            break
    else:
        pytest.fail("_kb_reranked is never assigned in search_endpoint")


def test_the_enrichment_call_sites_do_NOT_ask_for_the_meta():
    """They unpack nothing, so if a future edit made with_meta the default they would silently start
    treating a (rows, meta) tuple as a list of rows -- two 'chunks', one of them a dict of counts."""
    tree = ast.parse(inspect.getsource(main))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_query_kb_async"]
    assert len(calls) == 3, "expected 3 call sites, found %d" % len(calls)
    asked = [c for c in calls
             if any(k.arg == "with_meta" for k in c.keywords)]
    assert len(asked) == 1, (
        "exactly one call site should ask for the meta, found %d" % len(asked))
    # A line scan cannot do this: /search's call spans two lines, so `with_meta=True` sits on the
    # line AFTER the one naming the function. The first version of this test scanned lines and
    # reported zero -- it was measuring its own instrument, not the code.
    assert all(isinstance(k.value, ast.Constant) and k.value.value is True
               for c in asked for k in c.keywords if k.arg == "with_meta")
