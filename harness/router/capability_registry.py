"""
BIFROST Router -- Capability Registry loader (Phase 0a)
=======================================================
Per-alias capability metadata for routing decisions: tool-calling support,
context length, VRAM/throughput hints, profile tags, keep-alive policy.

Source of truth: capability_registry.json, hot-reloaded on mtime change (same
pattern as config._load_fleet_config). Seeded from fleet_config.json 2026-06-13.

Primary consumer: the tool-aware reroute guard in main.py (Phase 0b), which
refuses to route tool-calling requests to a tool_calls=false target and
re-resolves within the band to the nearest tool-capable alias.

Lookups resolve by alias key first, then by backend model name, then by tier
string -- so a caller holding any of the three (alias from the request, model
from TIER_BACKENDS, or a routed Tier value) can ask the same question.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Module path is overridable in tests (point at a temp file, reset _registry_mtime=0).
_REGISTRY_PATH = Path(__file__).parent / "capability_registry.json"

_registry_mtime: float = 0.0
_registry_raw: dict = {}
_by_alias: dict[str, dict] = {}
_by_model: dict[str, dict] = {}
_by_tier: dict[str, dict] = {}


def _rebuild_indexes(raw: dict) -> None:
    """Rebuild the alias/model/tier lookup indexes from a freshly loaded dict."""
    global _by_alias, _by_model, _by_tier
    by_alias: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_tier: dict[str, dict] = {}
    for entry in raw.get("aliases", []):
        alias = entry.get("alias")
        if alias:
            by_alias[alias] = entry
        model = entry.get("model")
        # First entry wins on a shared model name -- tool-capability is identical
        # across aliases that share a backend model (e.g. devstral:latest).
        if model and model not in by_model:
            by_model[model] = entry
        tier = entry.get("tier")
        if tier and tier not in by_tier:
            by_tier[tier] = entry
    _by_alias, _by_model, _by_tier = by_alias, by_model, by_tier


def _load_registry() -> dict:
    """Load capability_registry.json, caching until its mtime changes.

    Mirrors config._load_fleet_config: read-once on first call, hot-reload on
    mtime change. A missing file degrades to an empty registry (capability
    checks become no-ops); a malformed file keeps the last good cache.
    """
    global _registry_mtime, _registry_raw
    try:
        mtime = _REGISTRY_PATH.stat().st_mtime
        if mtime != _registry_mtime:
            with open(_REGISTRY_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips any BOM
                _registry_raw = json.load(f)
            _registry_mtime = mtime
            _rebuild_indexes(_registry_raw)
            log.info(
                f"capability_registry loaded: {len(_by_alias)} aliases "
                f"from {_REGISTRY_PATH.name}"
            )
    except FileNotFoundError:
        if _registry_mtime != 0.0 or _by_alias:
            log.warning(
                f"capability_registry.json vanished at {_REGISTRY_PATH} -- "
                f"keeping last good cache ({len(_by_alias)} aliases)"
            )
        else:
            log.warning(
                f"capability_registry.json not found at {_REGISTRY_PATH} -- "
                f"registry empty, capability checks are no-ops"
            )
            _registry_raw = {}
            _rebuild_indexes(_registry_raw)
    except Exception as e:
        log.warning(f"capability_registry.json load failed: {e} -- keeping last good cache")
    return _registry_raw


def get_alias(name: str) -> dict | None:
    """Return the capability entry for an alias, backend model name, or tier string.

    Resolution order: alias key -> model name -> tier string. Returns None if the
    name matches none of the three indexes (caller treats None as 'unknown').
    """
    if not name:
        return None
    _load_registry()
    return _by_alias.get(name) or _by_model.get(name) or _by_tier.get(name)


def is_tool_capable(name: str) -> bool:
    """True iff the named alias/model/tier is KNOWN to support tool-calling.

    Unknown names (absent from the registry) and explicit null tool_calls both
    return False. Callers needing the 'known-incapable' vs 'unknown' distinction
    (e.g. the reroute guard's trigger condition) must inspect get_alias() directly.
    """
    entry = get_alias(name)
    return bool(entry and entry.get("tool_calls"))


def get_ctx_len(name: str) -> int | None:
    """Context length for the named alias/model/tier, or None if unknown/unset."""
    entry = get_alias(name)
    if entry is None:
        return None
    v = entry.get("ctx_len")
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def list_by_tag(tag: str) -> list[str]:
    """Aliases whose profile_tags contain the given tag, in registry order."""
    _load_registry()
    return [
        e["alias"]
        for e in _registry_raw.get("aliases", [])
        if e.get("alias") and tag in (e.get("profile_tags") or [])
    ]


def get_practices(kind: str, families: set[str] | None = None, cap: int = 8) -> list[str]:
    """Practice-registry constraint texts for an authored-artifact kind (2026-07-29).

    The `practices` section holds MEASURED 8B-30B failure modes (each entry names the demo bank
    that taught it) so authoring prompts stop re-paying for pitfalls the fleet already named --
    see DESIGN-practice-registry-8b30b-2026-07-29. Selection is TAG-BASED and deterministic:
    `kind` must appear in the entry's applies_to (checker|code|tests|decompose) and `families`
    must intersect the entry's family tags ('any' always matches). Never semantic retrieval --
    that would reintroduce the per-take variance the registry exists to remove. Registry order,
    capped (small models follow short lists). Hot-reloads with the file like every other reader.
    """
    _load_registry()
    fams = set(families or ()) | {"any"}
    out: list[str] = []
    for e in _registry_raw.get("practices", []):
        if kind in (e.get("applies_to") or []) and fams & set(e.get("family") or ["any"]):
            t = (e.get("text") or "").strip()
            if t:
                out.append(t)
    return out[:cap]
