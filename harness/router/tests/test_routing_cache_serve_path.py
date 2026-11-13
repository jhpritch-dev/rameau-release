"""Tests for the Phase B request-path wiring (routing_cache_serve_path).

Driven with asyncio.run() from synchronous tests rather than an async plugin, so these run
under the repo's existing pytest configuration with no new dependency.

The load-bearing assertions here are about what does NOT happen: that a disabled feature does
not compute a hash or embed anything, that a hinted request never reaches the cache, and that
a cache-served request never gets written back. Those are the properties that make this safe
to deploy switched off.
"""
import asyncio

import pytest

import routing_cache as rc
import routing_cache_serve_path as sp


ON = {"BIFROST_ROUTING_CACHE_SERVE": "1"}


def run(coro):
    return asyncio.run(coro)


class Spy:
    """Counts calls so 'this never ran' is assertable, not assumed."""

    def __init__(self, result=None, raises=None, delay=0.0):
        self.result, self.raises, self.delay = result, raises, delay
        self.calls = 0

    async def __call__(self, *a, **kw):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.result


class StubCache:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    def lookup(self, vector, config_version):
        self.calls += 1
        return self.result


def hit(band="TRIVIAL", sim=0.97, conf=0.95):
    return rc.CacheLookupResult(hit=True, similarity=sim, cached_band=band,
                                cached_confidence=conf, cached_config_version="cfg1")


class Slot:
    def __init__(self, admitted=True):
        self.admitted = admitted
        self.failures = []

    def failed(self, why):
        self.failures.append(why)


def admit_factory(slot):
    class _CM:
        def __init__(self, name):
            self.name = name

        async def __aenter__(self):
            return slot

        async def __aexit__(self, *exc):
            return False
    return _CM


# --------------------------------------------------------------------------
# Disabled: must cost NOTHING. Not an embed, not even the config-version hash.
# --------------------------------------------------------------------------

def test_disabled_calls_classifier_and_nothing_else():
    classify = Spy(result=("CLS", {"routing_source": "npu"}))
    embed = Spy(result=[0.1])
    hashed = []

    out = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=StubCache(hit()), embed_fn=embed,
        config_version_fn=lambda: hashed.append(1) or "cfg1", env={}))

    assert out == ("CLS", {"routing_source": "npu"})
    assert classify.calls == 1
    assert embed.calls == 0, "disabled path must not embed"
    assert hashed == [], "disabled path must not compute the config-version hash"


def test_disabled_passes_arguments_through_untouched():
    seen = {}

    async def classify(prompt, file_count, hint):
        seen.update(prompt=prompt, file_count=file_count, hint=hint)
        return ("CLS", {})

    run(sp.serve_or_classify(prompt="hello", file_count=7, hint="complex",
                             classify_fn=classify, cache=StubCache(), embed_fn=Spy(),
                             config_version_fn=lambda: "cfg1", env={}))
    assert seen == {"prompt": "hello", "file_count": 7, "hint": "complex"}


# --------------------------------------------------------------------------
# Enabled and hitting: the classifier must be SKIPPED. That is the whole point.
# --------------------------------------------------------------------------

def test_hit_skips_the_classifier():
    classify = Spy(result=("CLS", {}))
    cache = StubCache(hit(band="COMPLEX"))
    cls, td = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=cache, embed_fn=Spy(result=[0.1]),
        config_version_fn=lambda: "cfg1", env=ON))

    assert classify.calls == 0, "a cache hit must not call the classifier"
    assert cache.calls == 1
    assert td["routing_source"] == "routing_cache"
    assert cls.band.value == "COMPLEX"


def test_routing_td_keys_match_the_rule_fallback_shape():
    """A served request must not raise a KeyError somewhere far downstream."""
    td = sp.routing_td(rc.ServeDecision(serve=True, reason="hit", band="TRIVIAL",
                                        confidence=0.9, similarity=0.99))
    for k in ("routing_source", "rule_pred", "npu_pred", "npu_conf",
              "npu_latency_ms", "fallback_reason", "agreed"):
        assert k in td


@pytest.mark.parametrize("band", ["TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"])
def test_classification_built_for_every_band(band):
    cls = sp.classification_from(
        rc.ServeDecision(serve=True, reason="hit", band=band, confidence=0.8, similarity=0.9))
    assert cls.band.value == band
    assert cls.confidence == 0.8
    assert cls.hint_override is False, "a served result is never hint-derived"


# --------------------------------------------------------------------------
# Every decline falls through to the classifier.
# --------------------------------------------------------------------------

def test_hint_bypasses_without_embedding():
    classify = Spy(result=("CLS", {}))
    embed = Spy(result=[0.1])
    run(sp.serve_or_classify(prompt="p", file_count=0, hint="complex",
                             classify_fn=classify, cache=StubCache(hit()), embed_fn=embed,
                             config_version_fn=lambda: "cfg1", env=ON))
    assert classify.calls == 1
    assert embed.calls == 0, "a hinted request must never reach the cache"


def test_embed_timeout_falls_through():
    classify = Spy(result=("CLS", {}))
    slow = Spy(result=[0.1], delay=0.20)
    out = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=StubCache(hit()), embed_fn=slow,
        config_version_fn=lambda: "cfg1", timeout_s=0.01, env=ON))
    assert out == ("CLS", {})
    assert classify.calls == 1


def test_timeout_budget_is_actually_enforced():
    d = run(sp.try_serve(prompt="p", cache=StubCache(hit()),
                         embed_fn=Spy(result=[0.1], delay=0.20),
                         config_version="cfg1", timeout_s=0.01, env=ON))
    assert d.serve is False
    assert d.reason == "embed_timeout"


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad"), OSError("net")])
def test_embed_error_falls_through(exc):
    classify = Spy(result=("CLS", {}))
    out = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=StubCache(hit()), embed_fn=Spy(raises=exc),
        config_version_fn=lambda: "cfg1", env=ON))
    assert out == ("CLS", {})
    assert classify.calls == 1


def test_cache_lookup_error_falls_through():
    class Exploding:
        def lookup(self, v, c):
            raise RuntimeError("cache is broken")

    classify = Spy(result=("CLS", {}))
    out = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=Exploding(), embed_fn=Spy(result=[0.1]),
        config_version_fn=lambda: "cfg1", env=ON))
    assert out == ("CLS", {})


def test_weak_band_declines_when_allowlist_narrowed():
    classify = Spy(result=("CLS", {}))
    env = dict(ON)
    env["BIFROST_ROUTING_CACHE_SERVE_BANDS"] = "TRIVIAL"
    out = run(sp.serve_or_classify(
        prompt="p", file_count=0, hint=None, classify_fn=classify,
        cache=StubCache(hit(band="FRONTIER")), embed_fn=Spy(result=[0.1]),
        config_version_fn=lambda: "cfg1", env=env))
    assert out == ("CLS", {})
    assert classify.calls == 1


# --------------------------------------------------------------------------
# Admission control -- more important here than in the shadow, because this now
# sits in front of every request instead of behind it.
# --------------------------------------------------------------------------

def test_admission_denied_declines_without_embedding():
    slot = Slot(admitted=False)
    embed = Spy(result=[0.1])
    d = run(sp.try_serve(prompt="p", cache=StubCache(hit()), embed_fn=embed,
                         config_version="cfg1", admit=admit_factory(slot), env=ON))
    assert d.serve is False and d.reason == "admission_denied"
    assert embed.calls == 0


def test_admission_granted_serves():
    slot = Slot(admitted=True)
    d = run(sp.try_serve(prompt="p", cache=StubCache(hit()), embed_fn=Spy(result=[0.1]),
                         config_version="cfg1", admit=admit_factory(slot), env=ON))
    assert d.serve is True


def test_breaker_is_told_before_the_failure_is_swallowed():
    """A breaker cannot learn from an exception it is never shown."""
    slot = Slot(admitted=True)
    d = run(sp.try_serve(prompt="p", cache=StubCache(hit()),
                         embed_fn=Spy(raises=RuntimeError("boom")),
                         config_version="cfg1", admit=admit_factory(slot), env=ON))
    assert d.serve is False
    assert slot.failures == ["RuntimeError"]


# --------------------------------------------------------------------------
# The caller's own error handling must keep working.
# --------------------------------------------------------------------------

def test_classifier_exception_propagates_untouched():
    """main.py catches this to return 503 in live mode / fall back in shadow mode."""
    classify = Spy(raises=RuntimeError("npu down"))
    with pytest.raises(RuntimeError, match="npu down"):
        run(sp.serve_or_classify(
            prompt="p", file_count=0, hint=None, classify_fn=classify,
            cache=StubCache(hit()), embed_fn=Spy(raises=OSError("no embed")),
            config_version_fn=lambda: "cfg1", env=ON))
