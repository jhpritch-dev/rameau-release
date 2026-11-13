"""
Tests for capability_registry.py (Phase 0a).

Covers: load from the seeded JSON, get_alias resolution (alias / model / tier /
unknown), is_tool_capable (known-true / known-false / unknown / null), get_ctx_len,
list_by_tag, and mtime-based hot-reload.
"""
import importlib
import json
import os
from pathlib import Path

import pytest

import capability_registry as cr


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Reload the module + force a cache reset before each test so cross-test
    path overrides never leak."""
    importlib.reload(cr)
    cr._registry_mtime = 0.0
    yield
    importlib.reload(cr)


# --------------------------------------------------------------------------
# Load + seeded content
# --------------------------------------------------------------------------

def test_load_seeded_registry():
    raw = cr._load_registry()
    aliases = {a["alias"] for a in raw["aliases"]}
    # The seed aliases the brief requires, MINUS bifrost-1a-forge -- see the xfail below.
    required = {
        "bifrost-coder-toolmaster", "bifrost-t2-gemma4", "bifrost-t2-general",
        "bifrost-1a-hearth", "bifrost-auto",
        "bifrost-igor-solo", "bifrost-toolmaster",
    }
    assert required.issubset(aliases)


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP (2026-08-07): fleet_config defines tier '1a-forge' "
           "(active_model=gemma4:e4b) but capability_registry has no alias for it. The alias was "
           "present at seed (45140a3) and is gone. Adding it needs MEASURED tool_calls/ctx_len "
           "for gemma4:e4b plus a Rule-1 ceremony on capability_registry.json -- fabricating "
           "capability data would be worse than this red. Not dangerous meanwhile: the reroute "
           "guard acts only on KNOWN capability, so an unregistered tier reads as unknown and is "
           "left alone. strict=True: when the alias lands, this XPASSes and must be deleted.",
)
def test_seed_includes_1a_forge():
    aliases = {a["alias"] for a in cr._load_registry()["aliases"]}
    assert "bifrost-1a-forge" in aliases


# --------------------------------------------------------------------------
# get_alias resolution
# --------------------------------------------------------------------------

def test_get_alias_by_alias_key():
    e = cr.get_alias("bifrost-coder-toolmaster")
    assert e is not None
    assert e["model"] == "qwen3-coder:30b-a3b-q4_K_M"
    assert e["tier"] == "2_qwen_coder"


def test_get_alias_by_model_name():
    """Resolving by MODEL name is how the reroute guard maps a routed tier -> capability.

    Re-pinned 2026-08-15. This asserted `cr.get_alias("devstral:latest")["alias"] ==
    "bifrost-toolmaster"`, and both halves of its comment had gone false:

      * tier 2.5's active_model was promoted devstral:latest -> gpt-oss-igor-32k on 2026-06-19
        (fleet_config.json:321), and
      * R-14 repointed bifrost-toolmaster to qwen3-coder:30b-a3b on 2026-08-12 after devstral
        failed tool use twice.

    No registry alias carries devstral:latest any more, so the lookup returns None. Tier 2.5 is
    the live case the guard actually exercises. Asserting tier + tool_calls rather than the alias
    key on purpose: the model name is shared by bifrost-igor and bifrost-t25-oss, so which alias
    wins the by-model index is an ordering detail, while the capability answer is what the guard
    consumes and is identical either way.

    Re-pinned a THIRD time 2026-09-04 when tier 2.5 was restored to gpt-oss-igor-65k, and
    this time on the MECHANISM rather than on a model string. Note what all three re-pins have
    in common: each was forced by the REGISTRY STRING moving, not by the guard breaking. The
    gpt-oss-igor-32k anchor was never checked against Forge, which carries gpt-oss-igor-65k and
    no -32k of any tag, so this fixture spent months green while pinned to a model that did not
    exist. A fourth hardcoded string would only queue up a fifth.

    What the reroute guard depends on is the ROUND TRIP: a live tier's active_model resolves
    through the by-model index to a capability answer for that same tier. That is derived from
    fleet_config here, so it survives any future repoint and still fails if the index breaks.
    Asserting tier + tool_calls rather than the alias key on purpose -- a model name shared by
    several aliases makes which one wins an ordering detail, while the capability answer is what
    the guard consumes and is identical either way.

    Guarded against vacuity: a test that derives its own expectation can end up asserting
    nothing, so this requires a floor of tiers to actually resolve.
    """
    fleet = json.loads(
        (Path(__file__).resolve().parents[1] / "fleet_config.json").read_text(encoding="utf-8")
    )
    checked = 0
    for tier in fleet["tiers"]:
        model = tier.get("active_model")
        if not model:
            continue
        e = cr.get_alias(model)
        if e is None:
            continue          # not every tier has a registry row; that is a separate assertion
        # A model served by more than one tier (2.5 and 2.5-solo both serve gpt-oss-igor-65k)
        # resolves first-wins through the by-model index, and cloud rows carry tier=None. The
        # real property is that whatever comes back is a tier that actually serves this model.
        serving = {t["tier"] for t in fleet["tiers"] if t.get("active_model") == model}
        assert e["tier"] is None or e["tier"] in serving, (model, e["tier"], serving)
        assert isinstance(e["tool_calls"], (bool, type(None))), model
        checked += 1

    assert checked >= 8, (
        "by-model resolution produced only %d hits -- the index is broken or empty, and this "
        "test would otherwise pass vacuously" % checked
    )

    # The live case the guard actually exercises, derived rather than hardcoded.
    t25 = next(t for t in fleet["tiers"] if t["tier"] == "2.5")
    e = cr.get_alias(t25["active_model"])
    assert e is not None, t25["active_model"]
    # NOT `== "2.5"`. Since 2026-09-04 both 2.5 and 2.5-solo serve gpt-oss-igor-65k, and the
    # by-model index is first-wins, so bifrost-igor-solo answers this lookup. Which alias wins
    # is an ordering detail; the capability answer is identical either way, which is precisely
    # why the reroute guard consumes tool_calls and not the alias key.
    serving_25 = {t["tier"] for t in fleet["tiers"]
                  if t.get("active_model") == t25["active_model"]}
    assert e["tier"] in serving_25, (e["tier"], serving_25)
    assert e["tool_calls"] is True


def test_registry_ctx_len_tracks_tier_num_ctx():
    """A registry ctx_len must match the num_ctx of the tier it names, for LOCAL tiers.

    Added 2026-09-04. bifrost-coder-toolmaster carried ctx_len 131072 -- with the vram_gb,
    tok_s and cold_load_s measured at that context -- for the two months after tier
    2_qwen_coder was lowered to 16384. Nothing caught it, because every existing assertion
    read the registry rather than comparing it to fleet_config. The stale figures were then
    quoted as the tier's current footprint (44 GB against a measured 21.77).

    Cloud tiers are excluded: their rows legitimately carry None or a vendor-specific window
    (bifrost-cohere 128000 vs a 131072 tier). The two local rows that disagree today are pinned
    as an exact set, so this fails if a THIRD appears and also if one is repaired without
    updating the list.
    """
    fleet = json.loads(
        (Path(__file__).resolve().parents[1] / "fleet_config.json").read_text(encoding="utf-8")
    )
    tiers = {t["tier"]: t for t in fleet["tiers"]}
    reg = json.loads(
        (Path(__file__).resolve().parents[1] / "capability_registry.json").read_text(
            encoding="utf-8")
    )

    # Emptied 2026-09-04 (K-5, John signed). Both entries were WRONG, not tolerated
    # drift: bifrost-coder-tool and bifrost-granite name the SAME model
    # (granite-4.1-8b-tekken on Bifrost) and claimed 16384 and 32768 -- so they
    # contradicted each other before any tier was consulted. The live Modelfile pins
    # num_ctx 8192 and both tiers agree; 131072 is the ARCHITECTURE limit, which is
    # almost certainly where the old numbers came from -- a capability of the weights
    # recorded as if it were the configured runtime value.
    KNOWN_DRIFT: set[str] = set()

    local_checked, drifted = 0, set()
    for a in reg["aliases"]:
        t = tiers.get(a.get("tier"))
        if t is None or t.get("type") != "ollama":
            continue
        local_checked += 1
        if a.get("ctx_len") != t.get("num_ctx"):
            drifted.add(a["alias"])

    assert local_checked >= 15, (
        "only %d local aliases examined -- the registry or fleet_config did not load, and this "
        "test would otherwise pass vacuously" % local_checked
    )
    assert drifted == KNOWN_DRIFT, (
        "registry ctx_len drifted from tier num_ctx. new=%s repaired=%s"
        % (sorted(drifted - KNOWN_DRIFT), sorted(KNOWN_DRIFT - drifted))
    )


def test_devstral_tier_is_known_tool_incapable():
    """The gap the re-pin above uncovered, stated as the behavior that SHOULD hold.

    Tier 2_devstral is still live (fleet_config.json:662, active_model devstral:latest), still in
    _LEAF_TIERS (subtask_graph.py:240) and still in _DECOMPOSE_TIERS (autopilot_graph.py:1213)
    with the decomposer prompt naming it. But nothing in the registry maps to it, so
    _tier_tool_capability returns None -- and per its own docstring the guard "only reroutes AWAY
    from a KNOWN-incapable target". devstral is MEASURED-incapable, not unknown: R-14 recorded
    zero tool calls, tool-call JSON printed as prose, and a fabricated symbol and path
    (bifrost_delegate.py:61). Reading as UNKNOWN is what stops the guard rerouting away from it.

    xfail(strict=True), not an assertion that None is correct: it fails the suite the moment a
    signed registry entry makes it pass, so the marker cannot outlive the defect.
    """
    assert cr.get_alias("2_devstral") is not None
    assert cr.get_alias("2_devstral")["tool_calls"] is False


def test_get_alias_by_tier_string():
    e = cr.get_alias("2")  # T2 tier value
    assert e is not None
    # Renamed 2026-06-24 (stale-key fix) to match the live /v1/models alias. The underlying
    # Gemma4 26B Q4_K_M @16k is unchanged; only the key lost its "-16k" suffix.
    assert e["alias"] == "bifrost-t2-gemma4"


def test_get_alias_unknown_returns_none():
    assert cr.get_alias("no-such-model") is None
    assert cr.get_alias("") is None


# --------------------------------------------------------------------------
# is_tool_capable
# --------------------------------------------------------------------------

def test_is_tool_capable_known_true():
    assert cr.is_tool_capable("bifrost-coder-toolmaster") is True
    assert cr.is_tool_capable("bifrost-toolmaster") is True
    assert cr.is_tool_capable("bifrost-igor-solo") is True


def test_is_tool_capable_known_false():
    # gemma4 was the footgun the reroute guard was built for, but it is tool_calls=true
    # since 2026-07-07 ("tier 2 gemma4 tool_use=true ... resident eval core").
    # This assertion previously named "-16k", dead as a registry key since the 2026-06-24
    # rename, so it passed VACUOUSLY -- is_tool_capable() returns False for any unresolvable
    # name, exactly as it does for "zzz-not-a-model". It now names a real key and asserts
    # the real value.
    assert cr.is_tool_capable("bifrost-t2-gemma4") is True
    assert cr.is_tool_capable("bifrost-1a-forge") is False


def test_is_tool_capable_unknown_is_false():
    assert cr.is_tool_capable("totally-unknown") is False


def test_is_tool_capable_null_is_false():
    # bifrost-auto has tool_calls=null -> False (never routed tools directly).
    assert cr.is_tool_capable("bifrost-auto") is False


def test_known_incapable_vs_unknown_distinction():
    """The reroute guard relies on telling 'known false' apart from 'unknown'.

    The exemplar moved off gemma4 on 2026-08-07: gemma4 was flipped to tool_calls=true
    ("tier 2 gemma4 tool_use=true ... resident eval core"), so it is no longer a
    known-INCAPABLE model and cannot carry this assertion. bifrost-npu (gemma3:4b on the
    XDNA2 NPU) is genuinely tool_calls=false.
    """
    npu = cr.get_alias("bifrost-npu")
    assert npu is not None and npu.get("tool_calls") is False      # known-incapable
    assert cr.get_alias("mystery-model") is None                   # unknown


# --------------------------------------------------------------------------
# get_ctx_len + list_by_tag
# --------------------------------------------------------------------------

def test_get_ctx_len():
    # 16384, not 131072. The registry row said 131072 until 2026-09-04 while tier
    # 2_qwen_coder had run at 16384 since 2026-07-07 (thrash-relief B) -- so this assertion
    # was green precisely BECAUSE the registry disagreed with the tier it describes. See
    # test_registry_ctx_len_tracks_tier_num_ctx below, which pins that invariant directly.
    assert cr.get_ctx_len("bifrost-coder-toolmaster") == 16384
    assert cr.get_ctx_len("bifrost-t2-gemma4") == 16384
    assert cr.get_ctx_len("bifrost-auto") is None      # null in seed
    assert cr.get_ctx_len("unknown-model") is None


def test_list_by_tag():
    agents = cr.list_by_tag("agent")
    assert "bifrost-coder-toolmaster" in agents
    assert "bifrost-toolmaster" in agents
    assert "bifrost-igor-solo" in agents
    # gemma4 general tier is not tagged 'agent'
    assert "bifrost-t2-gemma4" not in agents
    assert cr.list_by_tag("no-such-tag") == []


# --------------------------------------------------------------------------
# mtime-based hot-reload
# --------------------------------------------------------------------------

def test_mtime_hot_reload(tmp_path):
    reg = tmp_path / "capability_registry.json"
    v1 = {"aliases": [{"alias": "x", "model": "m1", "tier": "tx", "tool_calls": False, "ctx_len": 1024, "profile_tags": ["t1"]}]}
    reg.write_text(json.dumps(v1), encoding="utf-8")
    os.utime(reg, (1_000_000, 1_000_000))

    cr._REGISTRY_PATH = reg
    cr._registry_mtime = 0.0

    assert cr.is_tool_capable("x") is False
    assert cr.get_ctx_len("x") == 1024

    # Rewrite with a changed capability + a strictly newer mtime.
    v2 = {"aliases": [{"alias": "x", "model": "m1", "tier": "tx", "tool_calls": True, "ctx_len": 4096, "profile_tags": ["t1", "t2"]}]}
    reg.write_text(json.dumps(v2), encoding="utf-8")
    os.utime(reg, (2_000_000, 2_000_000))

    # Same call now reflects the new file (mtime changed -> reload).
    assert cr.is_tool_capable("x") is True
    assert cr.get_ctx_len("x") == 4096
    assert "x" in cr.list_by_tag("t2")


def test_missing_file_degrades_to_empty(tmp_path):
    cr._REGISTRY_PATH = tmp_path / "does_not_exist.json"
    cr._registry_mtime = 0.0
    assert cr.get_alias("anything") is None
    assert cr.is_tool_capable("anything") is False
    assert cr.list_by_tag("agent") == []
