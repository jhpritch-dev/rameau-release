"""Step 2a acceptance: the MoA pipeline is OPT-IN, and a direct tier address always wins.

Packet: coordination/pending/orch_apply.PENDING-pipeline-mode-opt-in-step2-2026-09-01.md
Defect: main.py:2056 sets `band = ComplexityBand.COMPLEX` UNCONDITIONALLY inside the
`_tier_override is not None` branch, as a nominal band for telemetry. main.py:2387 then fed
that placeholder to select_pipeline_stages, whose only gate was `band.value in trigger_bands`
(strategies.py:448). So addressing a tier directly was the one request shape GUARANTEED to be
captured by band-gated staging, on every request, whatever the classifier thought.

T1/T2/T3 do NOT re-implement the gate. They extract the literal gate text from main.py and
execute it, so a test that passes here cannot drift from the protected file. A copied
predicate would prove only that the copy works.
"""

import pathlib
import textwrap

import pytest

import strategies
from config import ComplexityBand, Tier

REPO = pathlib.Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"

GATE_HEAD = "    _pipeline_requested = ("
GATE_TAIL = ("    _pipeline_stages = select_pipeline_stages(band) "
             "if _pipeline_requested else None")
# The pre-Step-2 unguarded call. Its ABSENCE is what proves the edit landed.
UNGUARDED = "    _pipeline_stages = select_pipeline_stages(band)\n"


def _gate_source() -> str:
    """The verbatim opt-in gate, lifted out of main.py and dedented to module level."""
    src = MAIN.read_text(encoding="utf-8")  # universal newlines: CRLF -> \n
    assert src.count(GATE_HEAD) == 1, "gate head must appear exactly once in main.py"
    assert src.count(GATE_TAIL) == 1, "gate tail must appear exactly once in main.py"
    i = src.index(GATE_HEAD)
    j = src.index(GATE_TAIL) + len(GATE_TAIL)
    assert j > i, "gate tail must follow gate head"
    return textwrap.dedent(src[i:j])


class _Headers:
    """Minimal stand-in for starlette Headers: case-insensitive .get(), like the real one."""

    def __init__(self, mapping):
        self._m = {str(k).lower(): v for k, v in (mapping or {}).items()}

    def get(self, key, default=None):
        return self._m.get(str(key).lower(), default)


class _Request:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def run_gate(headers=None, body=None, tier_override=None,
             band=ComplexityBand.COMPLEX, select=None):
    """Execute main.py's real gate text against a controlled namespace."""
    calls = []

    def _spy(b):
        calls.append(b)
        return (select or strategies.select_pipeline_stages)(b)

    ns = {
        "request": _Request(headers),
        "body": dict(body or {}),
        "_tier_override": tier_override,
        "band": band,
        "pipeline_requested": strategies.pipeline_requested,
        "select_pipeline_stages": _spy,
    }
    exec(compile(_gate_source(), str(MAIN), "exec"), ns)
    return ns["_pipeline_requested"], ns["_pipeline_stages"], calls


# The shipped stage list, with `enabled` flipped -- i.e. exactly what Step 2b will produce.
LIVE_STAGES = [{"stage": 1, "tier": "2_qwen_coder", "role": "draft", "stop_on_confident": True}]


@pytest.fixture
def pipeline_enabled(monkeypatch):
    """ARM B: pipeline_mode.enabled = TRUE.

    Without this the whole suite is a passing negative control -- select_pipeline_stages
    returns None at its `enabled` guard before the band test is ever reached, so every
    assertion below would hold for a build in which nothing was implemented.
    """
    cfg = {"enabled": True, "trigger_bands": ["COMPLEX", "FRONTIER"], "stages": LIVE_STAGES}
    monkeypatch.setattr(strategies, "get_pipeline_config", lambda: cfg)
    monkeypatch.setattr(strategies, "get_fanout_config", lambda: {"enabled": False})
    assert strategies._moa_mode() == "pipeline", "fixture must actually arm the pipeline"
    assert strategies.select_pipeline_stages(ComplexityBand.COMPLEX) == LIVE_STAGES, \
        "fixture must make staging REACHABLE, else every test below is vacuous"
    return cfg


@pytest.fixture
def pipeline_disabled(monkeypatch):
    """ARM A: pipeline_mode.enabled = FALSE, pinned rather than borrowed from what ships.

    This fixture used to be absent and ARM A read the real fleet_config instead. When PHASE 2b
    shipped the pipeline ENABLED, the premise went false and the arm failed -- not because the
    behavior regressed, but because the test had hardcoded a config value as if it were a law.

    The self-check mirrors ARM B's: prove the fixture actually DISARMS the pipeline. Without it
    a build in which the enabled guard was deleted would still satisfy every assertion below.
    """
    cfg = {"enabled": False, "trigger_bands": ["COMPLEX", "FRONTIER"], "stages": LIVE_STAGES}
    monkeypatch.setattr(strategies, "get_pipeline_config", lambda: cfg)
    monkeypatch.setattr(strategies, "get_fanout_config", lambda: {"enabled": False})
    assert strategies._moa_mode() != "pipeline", "fixture must actually disarm the pipeline"
    assert strategies.select_pipeline_stages(ComplexityBand.COMPLEX) is None, \
        "fixture must make staging UNREACHABLE, else the arm proves nothing"
    return cfg


# ---------------------------------------------------------------------------
# T5 -- unit: truthy parsing is EXPLICIT, not bool(str)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header,body,expected", [
    # present and affirmative, header transport
    ("1", None, True),
    ("true", None, True),
    ("on", None, True),
    ("yes", None, True),
    ("TRUE", None, True),
    ("  1  ", None, True),
    # present and affirmative, body transport (clause (b), main.py:1784 precedent)
    (None, "true", True),
    (None, True, True),
    (None, 1, True),
    # absent
    (None, None, False),
    # present and negative -- every one of these is TRUTHY under bool(str)
    ("0", None, False),
    ("false", None, False),
    ("off", None, False),
    ("no", None, False),
    (None, False, False),
    (None, 0, False),
    # empty and garbage
    ("", None, False),
    ("   ", None, False),
    ("maybe", None, False),
    ("2", None, False),
    ("truthy", None, False),
    # either transport is sufficient
    ("0", "1", True),
    ("1", "0", True),
])
def test_t5_pipeline_requested_parsing(header, body, expected):
    assert strategies.pipeline_requested(header, body) is expected


def test_t5_parsing_is_not_bool_of_string():
    """The specific failure mode being excluded: `bool("0")` and `bool("false")` are True."""
    for v in ("0", "false", "off", "no", "maybe", "2"):
        assert bool(v) is True, "precondition: these are all truthy to bool()"
        assert strategies.pipeline_requested(v, None) is False


# ---------------------------------------------------------------------------
# T3 -- POSITIVE CONTROL: the guard is not dead on arrival
# ---------------------------------------------------------------------------

def test_t3_header_opt_in_selects_stages(pipeline_enabled):
    requested, stages, calls = run_gate(headers={"X-Bifrost-Pipeline": "1"})
    assert requested is True
    assert stages == LIVE_STAGES
    assert calls == [ComplexityBand.COMPLEX], "select_pipeline_stages must be consulted"


def test_t3_body_opt_in_selects_stages(pipeline_enabled):
    """Proves clause (b), not just clause (a)."""
    requested, stages, calls = run_gate(body={"pipeline": True})
    assert requested is True
    assert stages == LIVE_STAGES


def test_t3_header_is_case_insensitive_in_value(pipeline_enabled):
    requested, stages, _ = run_gate(headers={"x-bifrost-pipeline": "TRUE"})
    assert requested is True
    assert stages == LIVE_STAGES


# ---------------------------------------------------------------------------
# T1 -- NO-SIGNAL PATH UNCHANGED (the load-bearing test)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("band", list(ComplexityBand))
def test_t1_no_signal_never_stages_even_when_enabled(pipeline_enabled, band):
    """ARM B: the block is ENABLED and the band qualifies, and it still must not fire."""
    requested, stages, calls = run_gate(band=band)
    assert requested is False
    assert stages is None
    assert calls == [], "with no signal, select_pipeline_stages must not even be consulted"


@pytest.mark.parametrize("headers,body", [
    (None, None),
    ({}, {}),
    ({"X-Bifrost-Pipeline": "0"}, None),
    ({"X-Bifrost-Pipeline": ""}, None),
    ({"X-Bifrost-Pipeline": "maybe"}, None),
    (None, {"pipeline": False}),
    ({"X-Tier-Override": ""}, {"model": "bifrost-auto"}),
])
def test_t1_unrecognised_signal_is_no_signal(pipeline_enabled, headers, body):
    requested, stages, _ = run_gate(headers=headers, body=body)
    assert requested is False
    assert stages is None


@pytest.mark.parametrize("band", list(ComplexityBand))
def test_t1_matches_pre_change_when_pipeline_disabled(pipeline_disabled, band):
    """ARM A: with the pipeline OFF, post-change == pre-change.

    Pre-change, main.py called select_pipeline_stages(band) unconditionally. This asserts that
    call's result is None for every band while the pipeline is disabled, and that the new gate
    also yields None -- so the two are identical on the no-signal path.

    Renamed from test_t1_matches_today_under_shipped_config: "today" was the bug. The claim is
    about the DISABLED path and is now pinned by a fixture, so shipping the pipeline enabled
    (PHASE 2b, v405) can no longer falsify a statement that was never about today's config.
    """
    assert strategies.select_pipeline_stages(band) is None, "disabled config must be inert"
    _, stages, _ = run_gate(band=band)
    assert stages is None


@pytest.mark.parametrize("band", list(ComplexityBand))
def test_t1_shipped_config_stages_exactly_its_trigger_bands(band):
    """The one test here that reads what actually SHIPS -- and survives it changing.

    It asserts a relationship, not a value: stages are returned for exactly the bands listed in
    the shipped pipeline_mode.trigger_bands, and for no others. True with the pipeline off (no
    bands qualify, because the enabled guard short-circuits first) and true with it on, so a
    future flip cannot break it the way it broke the arm above. What it WOULD catch is the
    shipped config claiming to trigger on a band it does not actually stage.
    """
    cfg = strategies.get_pipeline_config() or {}
    qualifies = bool(cfg.get("enabled")) and band.value in (cfg.get("trigger_bands") or [])
    stages = strategies.select_pipeline_stages(band)
    assert (stages is not None) == qualifies, (
        "shipped config: enabled=%r trigger_bands=%r says band %s should%s stage, got %r"
        % (cfg.get("enabled"), cfg.get("trigger_bands"), band.value,
           "" if qualifies else " NOT", stages))


# ---------------------------------------------------------------------------
# T2 -- DIRECT ADDRESS ALWAYS HONORED (the test that would have caught the defect)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers,body", [
    ({"X-Bifrost-Pipeline": "1"}, None),          # T2b: alias + explicit opt-in
    ({"X-Bifrost-Pipeline": "true"}, None),
    (None, {"pipeline": True}),
    ({"X-Bifrost-Pipeline": "yes"}, {"pipeline": "on"}),
    (None, None),                                  # T2a: alias, no signal
])
@pytest.mark.parametrize("override", [Tier("2.5-solo"), Tier("2_qwen_coder"), Tier("1b")])
def test_t2_direct_tier_address_always_wins(pipeline_enabled, headers, body, override):
    """The adversarial case: caller names a tier AND asks for the pipeline. The tier wins.

    band is COMPLEX here on purpose -- that is precisely what main.py:2056 forces for every
    direct address, and pre-change it made this the request shape most certain to be staged.
    """
    requested, stages, calls = run_gate(
        headers=headers, body=body, tier_override=override, band=ComplexityBand.COMPLEX)
    assert requested is False, "a direct tier address must veto the opt-in"
    assert stages is None, "a directly addressed tier must never be substituted by staging"
    assert calls == [], "the band must not even reach select_pipeline_stages"


def test_t2_regression_probe_fails_against_pre_change_logic(pipeline_enabled):
    """Negative control: the OLD line stages this request, so the probe is not vacuous.

    If this assertion ever fails, the fixture is not arming staging and every T2 pass above
    is meaningless.
    """
    old_result = strategies.select_pipeline_stages(ComplexityBand.COMPLEX)
    assert old_result == LIVE_STAGES, (
        "pre-change behavior must be reproducible: band=COMPLEX (forced by main.py:2056 "
        "for every direct address) DID select stages")


# ---------------------------------------------------------------------------
# T6 -- source-level shape of the protected file
# ---------------------------------------------------------------------------

def test_t6_unguarded_call_is_gone_and_import_is_present():
    src = MAIN.read_text(encoding="utf-8")
    assert UNGUARDED not in src, "the unconditional band-gated call must not remain"
    assert src.count("select_pipeline_stages(band)") == 1, "exactly one (guarded) call site"
    assert src.count("_pipeline_requested") == 2, "assignment plus its single use"
    assert "pipeline_requested)" in src, "pipeline_requested must be imported from strategies"
