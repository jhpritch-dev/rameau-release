"""Tests for the resident-matrix and persistence changes (2026-09-02).

The dangerous failure here is SILENT: a resident matrix that goes stale returns neighbors from
an older version of the cache and still looks like a perfectly good hit. Nothing raises. So the
invalidation paths get tested directly rather than inferred from "lookup still works".

The second risk is the persistence format change. A live routing_cache.bin written in the old
format is on disk and currently serving; if load rejected it the cache would come up empty and
look like a cold start rather than an error.
"""
import pickle

import numpy as np
import pytest

import routing_cache as rc


def vec(seed, dim=768):
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)


def cache(tmp_path, **kw):
    return rc.RoutingCache(persist_path=tmp_path / "rc.bin", **kw)


# --------------------------------------------------------------------------
# The ratified size.
# --------------------------------------------------------------------------

def test_default_max_size_is_the_bumped_value():
    assert rc.DEFAULT_MAX_SIZE == 16384


# --------------------------------------------------------------------------
# Matrix invalidation. A stale matrix fails silently, so test it directly.
# --------------------------------------------------------------------------

def test_write_is_visible_to_the_next_lookup(tmp_path):
    """The whole risk of a resident matrix: a write that the next lookup cannot see."""
    c = cache(tmp_path)
    v1 = vec(1)
    assert c.lookup(v1, "cfg1").miss_reason == "empty_cache"
    c.write(vector=v1, band="TRIVIAL", confidence=0.9, config_version="cfg1")
    r = c.lookup(v1, "cfg1")
    assert r.hit is True
    assert r.similarity == pytest.approx(1.0, abs=1e-5)


def test_second_write_is_also_visible(tmp_path):
    """One invalidation working does not prove the flag is reset correctly afterwards."""
    c = cache(tmp_path)
    c.write(vector=vec(1), band="TRIVIAL", confidence=0.9, config_version="cfg1")
    c.lookup(vec(1), "cfg1")                      # forces a rebuild, clears the stale flag
    v2 = vec(2)
    c.write(vector=v2, band="COMPLEX", confidence=0.9, config_version="cfg1")
    r = c.lookup(v2, "cfg1")
    assert r.hit is True and r.cached_band == "COMPLEX"


def test_matrix_rows_track_entry_count(tmp_path):
    c = cache(tmp_path)
    for i in range(5):
        c.write(vector=vec(i), band="TRIVIAL", confidence=0.9, config_version="cfg1")
        snapshot, mat, norms = c._snapshot()
        assert len(snapshot) == i + 1
        assert mat.shape[0] == i + 1
        assert norms.shape[0] == i + 1


def test_eviction_invalidates_the_matrix(tmp_path):
    """Over-cap eviction replaces the entry list; the matrix must not outlive it."""
    c = cache(tmp_path, max_size=3)
    for i in range(3):
        c.write(vector=vec(i), band="TRIVIAL", confidence=0.9, config_version="cfg1")
    c.lookup(vec(0), "cfg1")                      # build the matrix at size 3
    c.write(vector=vec(99), band="FRONTIER", confidence=0.9, config_version="cfg1")
    snapshot, mat, _ = c._snapshot()
    assert len(snapshot) == 3 and mat.shape[0] == 3
    assert c.lookup(vec(99), "cfg1").cached_band == "FRONTIER"


def test_repeated_lookups_are_stable(tmp_path):
    """Reusing a resident matrix must not drift the answer."""
    c = cache(tmp_path)
    for i in range(20):
        c.write(vector=vec(i), band="TRIVIAL", confidence=0.9, config_version="cfg1")
    first = c.lookup(vec(7), "cfg1")
    for _ in range(10):
        r = c.lookup(vec(7), "cfg1")
        assert r.hit == first.hit
        assert r.similarity == pytest.approx(first.similarity, abs=1e-9)
        assert r.cached_band == first.cached_band


def test_empty_cache_still_reports_empty(tmp_path):
    assert cache(tmp_path).lookup(vec(1), "cfg1").miss_reason == "empty_cache"


# --------------------------------------------------------------------------
# Persistence: v2 round-trip, and v1 must still load.
# --------------------------------------------------------------------------

def test_flush_load_round_trip_v2(tmp_path):
    c = cache(tmp_path)
    for i in range(6):
        c.write(vector=vec(i), band="COMPLEX", confidence=0.77, config_version="cfgX")
    c.flush()
    c2 = cache(tmp_path)
    assert c2.load_from_disk() == 6
    r = c2.lookup(vec(3), "cfgX")
    assert r.hit is True and r.cached_band == "COMPLEX"
    assert r.cached_confidence == pytest.approx(0.77)


def test_flush_writes_vectors_as_an_array_not_python_floats(tmp_path):
    """The point of the format change: 768 float32s must not become 768 python floats."""
    c = cache(tmp_path)
    c.write(vector=vec(1), band="TRIVIAL", confidence=0.9, config_version="cfg1")
    c.flush()
    data = pickle.loads((tmp_path / "rc.bin").read_bytes())
    assert isinstance(data, dict) and data["format"] == 2
    assert isinstance(data["vectors"], np.ndarray)
    assert data["vectors"].dtype == np.float32
    assert "vector" not in data["meta"][0]


def test_v1_format_still_loads(tmp_path):
    """A live cache file in the OLD format is on disk and serving. Refusing it would look
    like a cold start rather than an error."""
    old = [{"vector": vec(i).tolist(), "band": "MODERATE", "confidence": 0.8,
            "config_version": "cfgOld", "timestamp": 1000.0 + i} for i in range(4)]
    (tmp_path / "rc.bin").write_bytes(pickle.dumps(old))
    c = cache(tmp_path)
    assert c.load_from_disk() == 4
    r = c.lookup(vec(2), "cfgOld")
    assert r.hit is True and r.cached_band == "MODERATE"


def test_load_honours_the_current_cap(tmp_path):
    """A file written under a larger cap must not silently reinstate it."""
    old = [{"vector": vec(i).tolist(), "band": "TRIVIAL", "confidence": 0.9,
            "config_version": "c", "timestamp": float(i)} for i in range(10)]
    (tmp_path / "rc.bin").write_bytes(pickle.dumps(old))
    c = cache(tmp_path, max_size=4)
    assert c.load_from_disk() == 4
    assert c.size() == 4
    # FIFO age-out keeps the NEWEST, matching write()
    assert c.lookup(vec(9), "c").hit is True


def test_load_makes_entries_searchable(tmp_path):
    """Loading replaces the entry list wholesale -- the matrix must be rebuilt from it."""
    c = cache(tmp_path)
    for i in range(3):
        c.write(vector=vec(i), band="TRIVIAL", confidence=0.9, config_version="cfg1")
    c.flush()
    c2 = cache(tmp_path)
    c2.load_from_disk()
    assert c2.lookup(vec(1), "cfg1").hit is True


def test_missing_file_starts_empty(tmp_path):
    assert cache(tmp_path).load_from_disk() == 0


def test_corrupt_file_starts_empty_rather_than_raising(tmp_path):
    (tmp_path / "rc.bin").write_bytes(b"not a pickle at all")
    c = cache(tmp_path)
    assert c.load_from_disk() == 0
    assert c.lookup(vec(1), "cfg1").miss_reason == "empty_cache"
