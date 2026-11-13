"""F6 + HUNK A: the FACT tier's hard-coded 1.0 must never displace semantic content, and the cosine
floor must not be applied to rows the cross-encoder already ranked and selected.

The KB service emits `"similarity": 1.0` on every FACT-tier row. It is a sentinel, not a cosine.
`_query_kb` used to slice `chunks[:top_k]` and only then filter on similarity, so FACT rows -- which
arrive first -- consumed every slot. That was F6.

HUNK A (2026-09-27) fixes the second half: `similarity` is round(1 - chroma_distance, 3) assigned at
pipeline.py:417 BEFORE the cross-encoder runs, and `rerank_chunks` (pipeline.py:143) sorts by its score
and never writes the score back. So the response's ORDER is the ranking and the FIELD is a pre-rerank
cosine -- two scales in one payload. Flooring or re-sorting by the field on a RERANKED response discards
rows the reranker preferred: measured over 25 live queries, 16% of queries lose their rank-1 row at
0.59, 72% at 0.66. The response's `reranked` flag is the only way to know which scale you have.

Fixtures below are shapes MEASURED against the live service at the decomposer's own parameters
(project bifrost-self, top_k=3), not invented ones:
  - 8 of 9 successful queries returned three FACT rows at 1.0 first, then semantic rows
    (observed semantic scores: 0.705, 0.72, 0.70, 0.657, 0.642, 0.629, 0.612, 0.54)
  - the nonsense control returned one FACT row at 1.0 with real content at 0.55 / 0.506
  - RERANKED_RANK1_LOW is "which machine hosts the knowledge base": the reranker's rank-1 row scores
    0.568, BELOW the floor, while its rank-2 row scores 0.669
"""
import inspect

import pytest

import subtask_graph as sg


def _fact(text, tier="FACT:P1"):
    return {"text": text, "tier": tier, "source": tier, "similarity": 1.0,
            "fact": {"value": text}}


def _sem(text, sim, source="pk__doc"):
    return {"text": text, "source": source, "similarity": sim}


# The majority live shape: three FACT rows at 1.0 FIRST, then semantic rows.
MEASURED = [
    _fact("fact-a"), _fact("fact-b"), _fact("fact-c"),
    _sem("real-070", 0.70), _sem("real-063", 0.63), _sem("real-054", 0.54),
]

# The nonsense control: one FACT row at 1.0, and nothing real clears the bar.
NONSENSE = [
    _fact("fact-z", "FACT:P2"),
    _sem("noise-055", 0.55), _sem("noise-0506", 0.506), _sem("noise-0506b", 0.506),
]

# Measured reranked payload whose RANK 1 row is below the floor. Cosines deliberately not descending:
# the list is in rerank order.
RERANKED_RANK1_LOW = [
    _sem("rank1-0568", 0.568, "whitepaper-v4_7"),
    _sem("rank2-0669", 0.669, "whitepaper-v2"),
    _sem("rank3-0564", 0.564, "whitepaper-v4_6"),
    _sem("rank4-0629", 0.629, "whitepaper-v3"),
    _sem("rank5-0572", 0.572, "whitepaper-v2b"),
]


def _old_expression(chunks, top_k):
    """The pre-F6 comprehension, verbatim in behavior: slice first, then filter at 0.66."""
    return [c["text"] for c in chunks[:top_k]
            if c.get("text") and isinstance(c.get("similarity"), (int, float))
            and c["similarity"] >= 0.66]


# ------------------------------------------------------------------ F6, unchanged by hunk A
def test_semantic_content_survives_the_measured_shape():
    """The defect: on this exact shape the old code returned zero semantic chunks."""
    got = sg._select_kb_chunks(MEASURED, top_k=3)
    assert "real-070" in got, got
    assert "real-063" in got, got


def test_the_old_expression_really_did_return_only_fact_rows():
    """Two-sided control. A fixture that does not discriminate before/after proves nothing."""
    old = _old_expression(MEASURED, top_k=3)
    assert old == ["fact-a", "fact-b", "fact-c"], old
    assert not any(t.startswith("real-") for t in old), old


def test_fact_rows_never_lead():
    got = sg._select_kb_chunks(MEASURED, top_k=6)
    first_fact = next((i for i, t in enumerate(got) if t.startswith("fact-")), len(got))
    last_real = max((i for i, t in enumerate(got) if t.startswith("real-")), default=-1)
    assert last_real < first_fact, got


def test_fact_rows_never_lead_on_the_reranked_path_either():
    """The partition is scale-independent: a sentinel is a sentinel however the rows were ordered."""
    got = sg._select_kb_chunks(MEASURED, top_k=6, reranked=True)
    first_fact = next((i for i, t in enumerate(got) if t.startswith("fact-")), len(got))
    last_real = max((i for i, t in enumerate(got) if t.startswith("real-")), default=-1)
    assert last_real < first_fact, got


def test_nonsense_control_returns_nothing_so_the_abstain_branch_fires():
    """Empty is the point: _execute_async's elif injects the NO-GROUNDING instruction."""
    assert sg._select_kb_chunks(NONSENSE, top_k=3) == []


def test_facts_are_withheld_when_there_is_no_semantic_row_at_all():
    only_facts = [_fact("f1"), _fact("f2")]
    assert sg._select_kb_chunks(only_facts, top_k=3) == []
    assert sg._select_kb_chunks(only_facts, top_k=3, reranked=True) == []


def test_top_k_is_applied_after_ordering_not_before_filtering():
    got = sg._select_kb_chunks(MEASURED, top_k=2)
    assert got == ["real-070", "real-063"], got


def test_below_bar_semantic_rows_are_dropped_but_still_count_for_relevance():
    got = sg._select_kb_chunks(MEASURED, top_k=6)
    assert "real-054" not in got, got


# ------------------------------------------------------------------ HUNK A: reranked -> trust order
def test_reranked_keeps_every_row_the_reranker_chose():
    got = sg._select_kb_chunks(RERANKED_RANK1_LOW, top_k=5, reranked=True)
    assert len(got) == 5, "the reranker's own selection was filtered again"


def test_reranked_preserves_service_order_and_does_NOT_sort_by_cosine():
    got = sg._select_kb_chunks(RERANKED_RANK1_LOW, top_k=5, reranked=True)
    assert got == [r["text"] for r in RERANKED_RANK1_LOW], got
    assert got[0] == "rank1-0568", "the reranker's rank 1 was displaced by a cosine sort"


def test_reranked_does_not_drop_the_rank_1_row_below_the_floor():
    """Measured: this happens on 16% of queries at 0.59 and 72% at 0.66."""
    assert "rank1-0568" in sg._select_kb_chunks(RERANKED_RANK1_LOW, top_k=5, reranked=True)


def test_reranked_ignores_the_floor_even_when_every_row_is_below_it():
    low = [_sem("a", 0.31), _sem("b", 0.28)]
    assert len(sg._select_kb_chunks(low, top_k=3, reranked=True)) == 2
    assert sg._select_kb_chunks(low, top_k=3) == []


def test_top_k_still_caps_the_reranked_path():
    assert len(sg._select_kb_chunks(RERANKED_RANK1_LOW, top_k=2, reranked=True)) == 2


def test_not_reranked_sorts_and_floors_exactly_as_before():
    got = sg._select_kb_chunks(RERANKED_RANK1_LOW, top_k=5)
    assert got == ["rank2-0669", "rank4-0629"], got


def test_the_new_params_are_keyword_only():
    """A positional third argument used to mean min_similarity. If `reranked` could be bound
    positionally, an un-updated caller would silently pass a FLOAT as the flag -- 0.66 is truthy, so
    the floor would switch off exactly where it was being tightened."""
    sig = inspect.signature(sg._select_kb_chunks)
    for name in ("reranked", "cosine_floor"):
        assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        sg._select_kb_chunks(MEASURED, 3, 0.66)


# ------------------------------------------------------------------ row-shape detection, unchanged
@pytest.mark.parametrize("row", [
    {"text": "by-fact-dict", "fact": {"v": 1}, "similarity": 1.0},
    {"text": "by-tier", "tier": "FACT:P2", "similarity": 1.0},
    {"text": "by-source", "source": "FACT:P1", "similarity": 1.0},
])
def test_fact_detected_by_each_of_its_three_markers(row):
    assert sg._is_fact_row(row) is True
    # and it is withheld when there is nothing semantic to carry it
    assert sg._select_kb_chunks([row], top_k=3) == []


@pytest.mark.parametrize("row", [
    {"text": "plain", "similarity": 0.9},
    {"text": "no-tier", "source": "pk__x", "similarity": 0.9},
    {"text": "tier-not-fact", "tier": "SEMANTIC", "similarity": 0.9},
])
def test_semantic_rows_are_not_mistaken_for_facts(row):
    assert sg._is_fact_row(row) is False
    assert sg._select_kb_chunks([row], top_k=3) == [row["text"]]


def test_junk_rows_do_not_raise_and_do_not_establish_relevance():
    junk = [None, "a string", 42, {}, {"text": ""},
            {"text": "no-sim"}, {"text": "bad-sim", "similarity": "abc"},
            {"text": "none-sim", "similarity": None}]
    assert sg._select_kb_chunks(junk, top_k=3) == []
    assert sg._select_kb_chunks(None, top_k=3) == []
    assert sg._select_kb_chunks([], top_k=3) == []


def test_junk_rows_with_text_SURVIVE_when_reranked():
    """No floor applies, so a row the service chose is kept even with an unusable cosine -- the
    reranked path must not depend on a field it has been told to ignore."""
    rows = [{"text": "bad-sim", "similarity": "abc"}, {"text": "none-sim", "similarity": None}]
    assert sg._select_kb_chunks(rows, top_k=3, reranked=True) == ["bad-sim", "none-sim"]
    assert sg._select_kb_chunks([None, "str", 42, {}], top_k=3, reranked=True) == []


def test_junk_alongside_a_real_hit_still_returns_the_hit():
    rows = [None, "str", {"text": "bad", "similarity": "abc"}, _sem("good", 0.8)]
    assert sg._select_kb_chunks(rows, top_k=3) == ["good"]


def test_similarity_coercion_never_raises():
    for bad in (None, "abc", {}, [], object()):
        assert sg._kb_similarity({"similarity": bad}) == 0.0
    assert sg._kb_similarity(None) == 0.0
    assert sg._kb_similarity({"similarity": "0.75"}) == 0.75


# ------------------------------------------------------------------ the constant
def test_the_floor_names_its_scale_and_is_session_o_parity():
    """0.59 is calibrated: on-topic min 0.607, nonsense max 0.577 (24 queries). The NAME matters --
    `_KB_MIN_SIMILARITY` reads as "a similarity floor", which is how five consumers came to floor a
    pre-rerank cosine believing it was the reranker's score."""
    assert sg._KB_COSINE_FLOOR == 0.59
    assert 0.577 < sg._KB_COSINE_FLOOR <= 0.607
    assert not hasattr(sg, "_KB_MIN_SIMILARITY"), "the misleading name is back"
    # The old 0.66 sat ABOVE real content worth keeping. 4 of the 9 measured queries had their
    # best semantic row below it (0.54, 0.612, 0.629, 0.642), so the bar itself discarded
    # on-topic passages independently of the slice-order defect.
    assert sg._KB_COSINE_FLOOR < 0.66
    at_066 = sg._select_kb_chunks(MEASURED, top_k=6, cosine_floor=0.66)
    at_059 = sg._select_kb_chunks(MEASURED, top_k=6)
    assert "real-063" not in at_066, at_066
    assert "real-063" in at_059, at_059


def test_a_query_whose_best_real_row_is_below_066_is_blacked_out_at_the_old_bar():
    """The 0.642 / 0.629 / 0.612 / 0.54 cases: 0.66 returns nothing real, 0.59 returns the row."""
    rows = [_fact("f"), _sem("real-0642", 0.642)]
    assert sg._select_kb_chunks(rows, top_k=3, cosine_floor=0.66) == []
    assert sg._select_kb_chunks(rows, top_k=3) == ["real-0642", "f"]


def test_ordering_is_by_descending_similarity_when_not_reranked():
    rows = [_sem("lo", 0.61), _sem("hi", 0.95), _sem("mid", 0.75)]
    assert sg._select_kb_chunks(rows, top_k=3) == ["hi", "mid", "lo"]


# ------------------------------------------------------------------ the wiring, by behavior
# 2026-10-13. The check that stood here matched SOURCE TEXT -- "_select_kb_chunks(chunks, top_k" in
# inspect.getsource(_query_kb) -- and went red on a correct tree when c16ea6b (Ceremony 108 Addendum A)
# split _query_kb into an entity arm and a description arm and its local became `_chunks`. The selector
# was still applied, once per arm. These drive the REAL _query_kb with /retrieve answered by a recorder,
# so a rename cannot fail them and a bypass cannot pass them.
_TOPIC = "the three-machine self-hosted inference fleet"


def _arm(tag, rows):
    """The same rows with texts unique to one arm (_query_kb de-duplicates across arms on text)."""
    return [dict(c, text="%s:%s" % (tag, c["text"])) for c in rows]


def _query_kb_with(monkeypatch, entity, described):
    """Run sg._query_kb(_TOPIC) offline. /retrieve answers `entity` to the entity query and `described`
    to the topic; each is a full response body, {"chunks": [...], "reranked": bool}."""
    import asyncio
    import httpx

    asked = []

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **k):
            asked.append(json["question"])
            return _Resp(entity if json["question"] == sg._KB_ENTITY_QUERY else described)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return asyncio.run(sg._query_kb(_TOPIC)), asked


def test_query_kb_still_delegates_to_the_selector(monkeypatch):
    """Wiring check: the fix is worthless if _query_kb does not apply it. Both arms answer with the
    measured FACT-first shape; without the selector the first injected text is a FACT sentinel (F6)."""
    out, asked = _query_kb_with(monkeypatch,
                                {"chunks": _arm("E", MEASURED), "reranked": True},
                                {"chunks": _arm("D", MEASURED), "reranked": True})
    assert asked == [sg._KB_ENTITY_QUERY, _TOPIC], asked
    assert out[:3] == ["E:real-070", "D:real-070", "E:real-063"], out
    assert not any(t.split(":", 1)[1].startswith("fact-") for t in out), out


def test_query_kb_abstains_when_no_arm_has_semantic_content(monkeypatch):
    """The NO-GROUNDING branch fires only on an empty list. A FACT-only arm and an arm whose every row is
    under the floor must both come back empty, or the leaf is told to answer from sentinels."""
    only_facts = [_fact("fact-a"), _fact("fact-b"), _fact("fact-c")]
    out, _ = _query_kb_with(monkeypatch,
                            {"chunks": _arm("E", only_facts), "reranked": True},
                            {"chunks": _arm("D", NONSENSE), "reranked": False})
    assert out == [], out


def test_query_kb_reads_reranked_from_each_response(monkeypatch):
    """Behavioral twin of the source check below. The same rows, reranked on one arm and not on the
    other: the reranked arm keeps its rank-1 row although its cosine is under the floor; the other arm
    floors it and re-sorts by cosine. A hardcoded True or a hardcoded False fails one side."""
    out, _ = _query_kb_with(monkeypatch,
                            {"chunks": _arm("E", RERANKED_RANK1_LOW), "reranked": True},
                            {"chunks": _arm("D", RERANKED_RANK1_LOW), "reranked": False})
    assert out[0] == "E:rank1-0568", out
    assert "D:rank1-0568" not in out, out
    assert out[1] == "D:rank2-0669", out


def test_no_fact_row_is_injected_ahead_of_a_semantic_row(monkeypatch):
    """2a64ac4's contract at the level the leaf sees: FACT rows only BEHIND every semantic row. Per-arm
    selection keeps it inside each arm; the entity-first interleave does not keep it across arms. Shape:
    the entity arm loses two of its three semantic rows to _drop_coordination_sources (Ceremony 110), so
    its selection is [semantic, FACT, FACT], and the interleave puts that FACT row third -- inside the
    default budget of 3 -- ahead of the description arm's second semantic row, which the trim then cuts."""
    entity = [_fact("fact-a"), _fact("fact-b"), _fact("fact-c"),
              _sem("coord-1", 0.80, "coordination__pending__X.md"),
              _sem("coord-2", 0.78, "OUTCOME-ceremony-999.md"),
              _sem("hw-spec", 0.74, "BIFROST-CURRENT-STATE.md")]
    described = [_fact("fact-d"), _fact("fact-e"), _fact("fact-f"),
                 _sem("prose-1", 0.70), _sem("prose-2", 0.66), _sem("prose-3", 0.62)]
    out, _ = _query_kb_with(monkeypatch,
                            {"chunks": _arm("E", entity), "reranked": True},
                            {"chunks": _arm("D", described), "reranked": True})
    kinds = ["fact" if t.split(":", 1)[1].startswith("fact-") else "sem" for t in out]
    first_fact = kinds.index("fact") if "fact" in kinds else len(kinds)
    assert "sem" not in kinds[first_fact:], out
    assert sg._trim_kb_chunks(out, "2_qwen_coder") == ["E:hw-spec", "D:prose-1", "D:prose-2"], out


def test_query_kb_passes_reranked_READ_FROM_THE_RESPONSE():
    """Hunk A's correctness rests on this being the response's own flag. A hardcoded True disables the
    floor for un-reranked responses; a hardcoded False reinstates the regression. Neither is visible
    to any behavioral test of the selector, because the selector stays correct and merely unused."""
    src = inspect.getsource(sg._query_kb)
    assert 'reranked=bool(data.get("reranked"))' in src, src
