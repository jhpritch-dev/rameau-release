"""main.py's KB row selection: the instrument depends on which SCALE the response is on. (D-1)

`/retrieve` embeds, fetches 50 candidates, reranks with a cross-encoder, keeps the best 5 and returns
them BEST-FIRST. `rerank_chunks` never writes the score back, and `similarity` is
round(1 - chroma_distance, 3) assigned at pipeline.py:417 BEFORE reranking. So the ORDER is the
cross-encoder's ranking and the FIELD is an embedding cosine -- two different scales.

Therefore:
  * reranked response  -> trust the order, apply NO cosine floor;
  * not reranked       -> the cosine is the only signal, so sort and floor by it.

Measured over 25 queries, applying a 0.59 cosine floor to a RERANKED response discards the reranker's
rank-1 row on 16% of queries and 18% of all selected rows (at 0.66: 72% and 72%, with 36% of queries
left with no context at all). That is the regression this file now pins.

Fixtures are measured live payloads, not invented shapes.
"""
import ast
import inspect

import pytest

import main


def _fact(obj):
    return {"similarity": 1.0, "tier": "FACT:P1", "source": "FACT:P1",
            "fact": {"subject": "s", "predicate": "amount", "object": obj},
            "text": "FACT sentinel " + obj, "chunk": 0}


def _sem(sim, src):
    return {"similarity": sim, "tier": None, "source": src, "fact": None,
            "text": "semantic body from " + src, "chunk": 0}


# measured: /retrieve "what does pk_sync do", top_k=5 -- note the cosines are NOT descending,
# because the list is in rerank order.
MEASURED = [
    _fact("$0.50"), _fact("$0.15"), _fact("$0.07"), _fact("$0.23"), _fact("$0.11"),
    _sem(0.66, "reports__INVESTIGATION_REPORT"),
    _sem(0.651, "pk__DISPATCH-JOURNAL-CONTRACT"),
    _sem(0.711, "coordination__done__applied"),
    _sem(0.699, "coordination__reference__B"),
    _sem(0.702, "pk__SMB-NAME-HIJACK-FIX"),
]

# measured: "which machine hosts the knowledge base" -- the reranker's RANK 1 row has cosine 0.568,
# BELOW the 0.59 floor. This payload is the regression, in one fixture.
RANK1_BELOW_FLOOR = [
    _sem(0.568, "whitepaper-v4_7"),   # reranker rank 1
    _sem(0.669, "whitepaper-v2"),     # rank 2
    _sem(0.564, "whitepaper-v4_6"),   # rank 3
    _sem(0.629, "whitepaper-v3"),     # rank 4
    _sem(0.572, "whitepaper-v2b"),    # rank 5
]


# ---------------------------------------------------------------- reranked: trust the order
def test_reranked_keeps_every_row_the_reranker_chose():
    """The reranker already selected the best 5 of 50. A second filter can only discard its work."""
    got = main._select_kb_rows(RANK1_BELOW_FLOOR, 5, reranked=True)
    assert len(got) == 5
    assert [c["source"] for c in got] == [c["source"] for c in RANK1_BELOW_FLOOR]


def test_reranked_preserves_service_order_and_does_NOT_sort_by_cosine():
    """THE regression test. Sorting by cosine replaces a cross-encoder ranking with an embedding
    one; on this measured payload it would promote rank 2 (0.669) over rank 1 (0.568)."""
    got = main._select_kb_rows(RANK1_BELOW_FLOOR, 5, reranked=True)
    assert got[0]["source"] == "whitepaper-v4_7", "the reranker's rank 1 was displaced"
    cosines = [c["similarity"] for c in got]
    assert cosines != sorted(cosines, reverse=True), "rows were re-sorted by cosine"


def test_reranked_does_not_drop_the_rank_1_row_below_the_floor():
    """Measured: this happens on 16% of queries at 0.59. It must not happen at all now."""
    got = main._select_kb_rows(RANK1_BELOW_FLOOR, 5, reranked=True)
    assert any(c["source"] == "whitepaper-v4_7" for c in got)


def test_reranked_ignores_the_cosine_floor_entirely_even_when_every_row_is_low():
    """A whole reranked set below the floor must still be returned -- the floor is not its scale."""
    low = [_sem(0.31, "a"), _sem(0.28, "b")]
    assert len(main._select_kb_rows(low, 5, reranked=True)) == 2
    assert main._select_kb_rows(low, 5, reranked=False) == []


# ---------------------------------------------------------------- not reranked: cosine is all there is
def test_not_reranked_sorts_by_cosine_descending():
    sims = [c["similarity"] for c in main._select_kb_rows(MEASURED, 5, reranked=False)]
    assert sims == sorted(sims, reverse=True)
    assert sims[0] == 0.711


def test_not_reranked_applies_the_floor():
    assert main._select_kb_rows([_fact("$1"), _sem(0.58, "a")], 5, reranked=False) == []
    got = main._select_kb_rows([_fact("$1"), _sem(0.60, "a")], 5, reranked=False)
    assert [c["similarity"] for c in got if not main._kb_is_fact_row(c)] == [0.60]


def test_not_reranked_drops_weak_rows_a_strong_one_would_otherwise_carry():
    got = main._select_kb_rows([_sem(0.90, "strong"), _sem(0.40, "weak")], 5, reranked=False)
    assert [c["source"] for c in got] == ["strong"]


def test_the_default_is_the_conservative_branch():
    """reranked defaults to False, so a caller that forgets gets the floor rather than an
    unfiltered injection. The caller is still asserted to pass it explicitly, below."""
    assert inspect.signature(main._select_kb_rows).parameters["reranked"].default is False


# ---------------------------------------------------------------- scale-independent, unchanged by D-1
def test_a_fact_only_payload_returns_nothing_on_either_branch():
    facts = [_fact("$1"), _fact("$2"), _fact("$3")]
    assert main._select_kb_rows(facts, 5, reranked=True) == []
    assert main._select_kb_rows(facts, 5, reranked=False) == []


def test_fact_rows_rank_below_real_content():
    payload = [_sem(0.80, "a"), _fact("$1"), _fact("$2")]
    got = main._select_kb_rows(payload, 5, reranked=True)
    assert len(got) == 3
    assert not main._kb_is_fact_row(got[0])
    assert all(main._kb_is_fact_row(c) for c in got[1:])


def test_top_k_caps_the_measured_100_row_payload():
    """top_k=50 measured 100 rows (50 semantic + 50 FACT); the FACT tier is additive."""
    big = ([_sem(0.70, "s%d" % i) for i in range(50)] + [_fact("$%d" % i) for i in range(50)])
    assert len(main._select_kb_rows(big, 5, reranked=True)) == 5
    assert len(main._select_kb_rows(big, 50, reranked=True)) == 50


def test_returns_dicts_carrying_source_and_text_not_texts():
    got = main._select_kb_rows(MEASURED, 5, reranked=True)
    assert got and all(isinstance(c, dict) for c in got)
    for c in got:
        assert "source" in c and "text" in c


def test_the_constant_names_its_scale():
    """The old name was `_KB_MIN_SIMILARITY`, which reads as 'a similarity floor' and is exactly how
    four consumers came to floor a cosine believing it was a rerank score. The scale is in the name."""
    assert main._KB_COSINE_FLOOR == 0.59
    assert not hasattr(main, "_KB_MIN_SIMILARITY"), "the misleading name is back"


@pytest.mark.parametrize("bad", [
    None, [], [{}], "not a list", [None], [{"text": ""}],
    [{"text": "x", "similarity": None}],
    [{"text": "x", "similarity": "high"}],
    [{"text": "x", "similarity": 0.9, "fact": "truthy-but-not-a-dict"}],
    ["a string row"],
])
@pytest.mark.parametrize("rr", [True, False])
def test_malformed_input_returns_a_list_and_never_raises(bad, rr):
    assert isinstance(main._select_kb_rows(bad, 5, reranked=rr), list)


def test_is_fact_row_recognises_both_shapes_the_service_uses():
    assert main._kb_is_fact_row({"fact": {"subject": "s"}}) is True
    assert main._kb_is_fact_row({"tier": "FACT:P1"}) is True
    assert main._kb_is_fact_row({"source": "FACT:P1"}) is True
    assert main._kb_is_fact_row({"tier": None, "source": "reports__X", "fact": None}) is False
    assert main._kb_is_fact_row("not a dict") is False
    assert main._kb_is_fact_row(None) is False


# ---------------------------------------------------------------- the wiring, which is the guarantee
def test_query_kb_async_returns_the_SELECTOR_result_not_the_raw_payload():
    """Load-bearing. The guarantee is that no consumer is reached unfiltered, and the way it breaks
    is an edit returning `raw` -- which no behavioral test of the selector would catch, because the
    selector would still be correct and merely unused."""
    src = inspect.getsource(main._query_kb_async)
    fn = ast.parse("".join(src.splitlines(keepends=True)).lstrip()).body[0]

    by_selector = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            if isinstance(f, ast.Name) and f.id == "_select_kb_rows":
                by_selector.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert by_selector, "_query_kb_async never calls _select_kb_rows"

    returned = [n.value.id for n in ast.walk(fn)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)]
    assert returned, "no bare-name return found"
    for name in returned:
        assert name in by_selector, (
            "_query_kb_async returns %r, not the selector's result" % name)


def test_the_caller_passes_reranked_READ_FROM_THE_PAYLOAD():
    """D-1's whole correctness rests on `reranked` being the response's own flag. A hardcoded True
    would silently disable the floor for un-reranked responses; a hardcoded False reinstates the
    regression. So the call must pass a name, and that name must come from the payload."""
    src = inspect.getsource(main._query_kb_async)
    fn = ast.parse("".join(src.splitlines(keepends=True)).lstrip()).body[0]

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_select_kb_rows"]
    assert len(calls) == 1, "expected exactly one _select_kb_rows call"
    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "reranked" in kw, "_select_kb_rows called without reranked= -- the default would apply"
    assert isinstance(kw["reranked"], ast.Name), (
        "reranked must be a variable read from the payload, not a literal")

    # and that variable must be assigned from the response, not invented
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == kw["reranked"].id for t in n.targets)]
    assert assigns, "the reranked flag is never assigned"
    assert "reranked" in ast.dump(assigns[0].value), (
        "the flag is not read from the response's 'reranked' field")


def test_only_one_place_posts_to_the_kb():
    """A second direct POST would bypass the selector entirely. Matched on the call expression, not
    the bare path: counting "/retrieve" also matches the comments that explain it, which is how the
    first version of this assertion failed on a correct file."""
    assert inspect.getsource(main).count("{kb_url}/retrieve") == 1
