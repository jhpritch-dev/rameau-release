"""Tests for the Phase B routing-cache serve path (routing_cache.serve).

The serve path is a GATE, and a gate needs two proofs, not one: that it stays shut
when it should (negative control) and that it can actually open (positive control).
A guard that has never been shown to fire is not a guard.

Band policy is FOUR LEVELS -- TRIVIAL, MODERATE, COMPLEX, FRONTIER -- ratified by
John 2026-09-01. The band gate is still exercised here, via a narrowed override, so
that widening the default did not leave the decline path untested.

No numpy and no real cache: serve() only orchestrates policy around cache.lookup(),
so a stub lookup isolates the policy from the vector maths.
"""
import pytest

import routing_cache as rc


ALL_BANDS = ["TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"]


class StubCache:
    """Records whether lookup was called at all -- "no lookup happened" is itself
    an assertion for the hint-bypass and disabled paths, where the point is that
    we must not pay for a lookup we are going to discard."""

    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises
        self.calls = []

    def lookup(self, vector, config_version):
        self.calls.append((vector, config_version))
        if self._raises is not None:
            raise self._raises
        return self._result


def hit(band="TRIVIAL", sim=0.97, conf=0.95, cfgv="cfg1"):
    return rc.CacheLookupResult(hit=True, similarity=sim, cached_band=band,
                                cached_confidence=conf, cached_config_version=cfgv)


def miss(reason, band=None, sim=0.5):
    return rc.CacheLookupResult(hit=False, similarity=sim, cached_band=band,
                                miss_reason=reason)


ON = {"BIFROST_ROUTING_CACHE_SERVE": "1"}


# --------------------------------------------------------------------------
# serve_enabled -- must fail closed. bool("0") and bool("false") are both True,
# so anything parsing by truthiness would turn "off" into "on".
# --------------------------------------------------------------------------

def test_enabled_unset_is_off():
    assert rc.serve_enabled({}) is False


@pytest.mark.parametrize("v", ["1", "true", "on", "yes", "TRUE", "Yes", "  on  "])
def test_enabled_truthy(v):
    assert rc.serve_enabled({rc.SERVE_ENV_VAR: v}) is True


@pytest.mark.parametrize("v", ["0", "false", "off", "no", "", "maybe", "2", "-1", "enabled"])
def test_enabled_falsy(v):
    assert rc.serve_enabled({rc.SERVE_ENV_VAR: v}) is False


# --------------------------------------------------------------------------
# serve_bands -- FOUR levels by default. A malformed override must never
# silently narrow OR widen away from the ratified policy.
# --------------------------------------------------------------------------

def test_bands_default_is_all_four_levels():
    assert rc.serve_bands({}) == frozenset(ALL_BANDS)


def test_default_constant_matches_the_four_level_policy():
    """Pins the ratified policy itself, so a later edit to the constant has to
    change a test that says why -- not slip through as a one-word diff."""
    assert rc.DEFAULT_SERVE_BANDS == frozenset(ALL_BANDS)


def test_bands_explicit_override():
    assert rc.serve_bands({rc.SERVE_BANDS_ENV_VAR: "trivial, moderate"}) == \
        frozenset({"TRIVIAL", "MODERATE"})


def test_bands_unknown_names_dropped_known_kept():
    assert rc.serve_bands({rc.SERVE_BANDS_ENV_VAR: "TRIVIAL,BOGUS"}) == frozenset({"TRIVIAL"})


@pytest.mark.parametrize("v", ["", "   ", "BOGUS", ",,,", "EXPERT,SIMPLE"])
def test_bands_all_unknown_falls_back_to_default_not_something_else(v):
    assert rc.serve_bands({rc.SERVE_BANDS_ENV_VAR: v}) == rc.DEFAULT_SERVE_BANDS


# --------------------------------------------------------------------------
# NEGATIVE CONTROL: flag off must be inert even on a perfect hit, and must not
# even perform the lookup.
# --------------------------------------------------------------------------

def test_disabled_does_not_serve_a_perfect_hit():
    cache = StubCache(hit(band="TRIVIAL", sim=1.0, conf=1.0))
    d = rc.serve(cache, [0.1], "cfg1", env={})
    assert d.serve is False
    assert d.reason == "disabled"


def test_disabled_performs_no_lookup():
    cache = StubCache(hit())
    rc.serve(cache, [0.1], "cfg1", env={})
    assert cache.calls == []


@pytest.mark.parametrize("v", ["0", "false", "off", "no", "maybe"])
def test_falsy_flag_values_do_not_open_the_gate(v):
    cache = StubCache(hit())
    d = rc.serve(cache, [0.1], "cfg1", env={rc.SERVE_ENV_VAR: v})
    assert d.serve is False and d.reason == "disabled"
    assert cache.calls == []


# --------------------------------------------------------------------------
# POSITIVE CONTROL: the gate must be able to open on ALL FOUR levels, or every
# test above is passing for the wrong reason.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("band", ALL_BANDS)
def test_every_band_is_served_by_default(band):
    cache = StubCache(hit(band=band, sim=0.93, conf=0.88))
    d = rc.serve(cache, [0.1], "cfg1", env=ON)
    assert d.serve is True
    assert d.reason == "hit"
    assert d.band == band
    assert d.confidence == 0.88
    assert d.similarity == 0.93
    assert len(cache.calls) == 1


def test_frontier_is_servable():
    """Explicit, because an earlier draft excluded FRONTIER on its 68.55% accuracy.
    FRONTIER is the top rank, so a FRONTIER answer cannot name a weaker band: its
    downgrade rate is 0.00% by construction and its errors are over-provisioning
    only. Excluding it guarded against a failure that cannot occur."""
    cache = StubCache(hit(band="FRONTIER"))
    assert rc.serve(cache, [0.1], "cfg1", env=ON).serve is True


def test_served_decision_carries_the_lookup_for_audit():
    cache = StubCache(hit())
    d = rc.serve(cache, [0.1], "cfg1", env=ON)
    assert d.lookup is not None and d.lookup.hit is True


def test_lookup_receives_the_config_version_it_was_given():
    cache = StubCache(hit())
    rc.serve(cache, [0.1], "cfgXYZ", env=ON)
    assert cache.calls[0][1] == "cfgXYZ"


# --------------------------------------------------------------------------
# Band gate -- widening the default to four levels must not leave the decline
# path dead. It is still reachable, and still tested, via a narrowed override.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("band", ["MODERATE", "FRONTIER", "COMPLEX"])
def test_band_gate_still_fires_when_narrowed(band):
    cache = StubCache(hit(band=band, sim=0.99, conf=0.99))
    env = dict(ON)
    env[rc.SERVE_BANDS_ENV_VAR] = "TRIVIAL"
    d = rc.serve(cache, [0.1], "cfg1", env=env)
    assert d.serve is False
    assert d.reason == "band_not_served"
    assert d.band == band          # still reported, so the decline is auditable


def test_narrowing_to_moderate_only_is_honoured():
    """MODERATE carries an 11.16% downgrade rate -- nine times any other band.
    If that shows up in practice the operational fix is to narrow this env var,
    with no rebuild, so the narrow path has to keep working."""
    env = dict(ON)
    env[rc.SERVE_BANDS_ENV_VAR] = "TRIVIAL,COMPLEX,FRONTIER"
    assert rc.serve(StubCache(hit(band="MODERATE")), [0.1], "cfg1", env=env).serve is False
    assert rc.serve(StubCache(hit(band="COMPLEX")), [0.1], "cfg1", env=env).serve is True


def test_narrowed_gate_still_requires_the_master_switch():
    """Narrowing must not accidentally act as an enable."""
    env = {rc.SERVE_BANDS_ENV_VAR: "TRIVIAL"}
    d = rc.serve(StubCache(hit()), [0.1], "cfg1", env=env)
    assert d.serve is False and d.reason == "disabled"


# --------------------------------------------------------------------------
# Hint bypass -- carried forward verbatim from Phase A.
# --------------------------------------------------------------------------

def test_hint_bypasses_and_performs_no_lookup():
    cache = StubCache(hit())
    d = rc.serve(cache, [0.1], "cfg1", hint_present=True, env=ON)
    assert d.serve is False
    assert d.reason == "hint_bypass"
    assert cache.calls == []


@pytest.mark.parametrize("band", ALL_BANDS)
def test_hint_bypass_beats_every_band(band):
    cache = StubCache(hit(band=band))
    assert rc.serve(cache, [0.1], "cfg1", hint_present=True, env=ON).reason == "hint_bypass"


# --------------------------------------------------------------------------
# Miss reasons propagate unchanged, so shadow analysis keeps working.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["below_threshold", "below_floor",
                                    "config_mismatch", "empty_cache"])
def test_miss_reason_propagates(reason):
    cache = StubCache(miss(reason, band="TRIVIAL"))
    d = rc.serve(cache, [0.1], "cfg1", env=ON)
    assert d.serve is False
    assert d.reason == reason


def test_miss_with_no_reason_is_labelled_not_blank():
    cache = StubCache(rc.CacheLookupResult(hit=False, similarity=0.4))
    d = rc.serve(cache, [0.1], "cfg1", env=ON)
    assert d.serve is False and d.reason == "miss"


# --------------------------------------------------------------------------
# Fail closed. A cache fault must never fail a request.
# --------------------------------------------------------------------------

def test_no_vector_is_a_clean_decline():
    cache = StubCache(hit())
    d = rc.serve(cache, None, "cfg1", env=ON)
    assert d.serve is False and d.reason == "no_vector"
    assert cache.calls == []


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad vector"),
                                 MemoryError(), KeyError("k")])
def test_lookup_exception_falls_through_and_does_not_escape(exc):
    cache = StubCache(raises=exc)
    d = rc.serve(cache, [0.1], "cfg1", env=ON)
    assert d.serve is False
    assert d.reason == "lookup_error"


def test_every_decline_reason_is_non_empty():
    """A decision that cannot say why it went the way it did is not auditable."""
    narrow = dict(ON)
    narrow[rc.SERVE_BANDS_ENV_VAR] = "TRIVIAL"
    cases = [
        (StubCache(hit()), [0.1], {}),
        (StubCache(hit()), None, ON),
        (StubCache(hit(band="FRONTIER")), [0.1], narrow),
        (StubCache(miss("below_floor")), [0.1], ON),
        (StubCache(raises=RuntimeError()), [0.1], ON),
    ]
    for cache, vec, env in cases:
        d = rc.serve(cache, vec, "cfg1", env=env)
        assert d.serve is False
        assert isinstance(d.reason, str) and d.reason
