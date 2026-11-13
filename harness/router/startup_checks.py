"""
v116 startup checks: drift orphan (STRICT) + env-var presence (WARN).
Called from main.py FastAPI lifespan startup.

Drift orphan = fleet_config tier string that has no matching Tier enum value.
That's a code/config invariant (silent drift is a bug); STRICT = raise + refuse to serve.

Env-var presence = OS env var named by PROVIDER_DEFAULTS[provider]["env_key"] is set
and non-empty. That's a runtime config check (missing key just makes the tier unreachable);
WARN = log + continue. v117 will extend to actual HTTP probes against base_url.
"""
import logging
import os
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def check_drift_orphans(fleet_config: Dict[str, Any], tier_values: List[str]) -> Dict[str, Any]:
    """STRICT: fleet_config tiers must all map to Tier enum values. Raises on fail."""
    config_tiers = {t["tier"] for t in fleet_config.get("tiers", [])}
    enum_values = set(tier_values)
    orphans = sorted(config_tiers - enum_values)
    status = {
        "check": "drift_orphans",
        "level": "STRICT",
        "config_tier_count": len(config_tiers),
        "enum_value_count": len(enum_values),
        "orphan_count": len(orphans),
        "orphans": orphans,
        "passed": len(orphans) == 0,
    }
    if orphans:
        log.error(f"startup_check check=drift_orphans status=FAIL orphans={orphans}")
        raise RuntimeError(f"fleet_config tier drift: orphans not in Tier enum: {orphans}")
    log.info(f"startup_check check=drift_orphans status=PASS tiers={len(config_tiers)}")
    return status


def check_env_vars(provider_defaults: Dict[str, Dict[str, Any]], fleet_config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """WARN: for each provider with an env-var field, verify env var is set + non-empty.
    Never raises. Field-name fallback chain prefers env_key (current PROVIDER_DEFAULTS
    convention as of v115), then accepts the alternate names from the original brief
    proposal so future provider entries with any of those keys still work.

    v117: fleet_config (optional) is consulted to surface the escalation_only flag per
    provider, so /health/backends consumers can distinguish always-on providers from
    cloud-escalation providers."""
    # v117: build provider -> escalation_only map from fleet_config tier definitions
    provider_to_esc: Dict[str, bool] = {}
    if fleet_config:
        for tier in fleet_config.get("tiers", []):
            prov = tier.get("provider")
            if prov:
                provider_to_esc[prov] = bool(tier.get("escalation_only", False))

    providers_state = []
    missing = []
    for provider, defaults in provider_defaults.items():
        env_var = (
            defaults.get("env_key")
            or defaults.get("auth_env")
            or defaults.get("env")
            or defaults.get("api_key_env")
        )
        esc = provider_to_esc.get(provider, False)
        if not env_var:
            providers_state.append({"provider": provider, "auth_env": None, "status": "NO_AUTH_REQUIRED", "escalation_only": esc})
            log.info(f"startup_check check=env_var provider={provider} status=NO_AUTH_REQUIRED escalation_only={esc}")
            continue
        value = os.environ.get(env_var, "")
        if not value:
            providers_state.append({"provider": provider, "auth_env": env_var, "status": "MISSING", "escalation_only": esc})
            missing.append(provider)
            log.warning(f"startup_check check=env_var provider={provider} env={env_var} status=MISSING escalation_only={esc}")
        else:
            providers_state.append({"provider": provider, "auth_env": env_var, "status": "PRESENT", "length": len(value), "escalation_only": esc})
            log.info(f"startup_check check=env_var provider={provider} env={env_var} status=PRESENT length={len(value)} escalation_only={esc}")
    return {
        "check": "env_vars",
        "level": "WARN",
        "providers": providers_state,
        "missing_count": len(missing),
        "missing": missing,
        "passed": len(missing) == 0,
    }


def run_all_startup_checks(
    fleet_config: Dict[str, Any],
    tier_values: List[str],
    provider_defaults: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Run drift (STRICT, raises) + env_vars (WARN, logs). Returns combined status for
    /health/backends consumption. v117: also surfaces global air_gapped_mode state."""
    drift = check_drift_orphans(fleet_config, tier_values)
    env = check_env_vars(provider_defaults, fleet_config)  # v117: pass fleet_config for escalation_only enrichment
    air_gapped = os.environ.get("BIFROST_AIR_GAPPED", "false").strip().lower() == "true"
    log.info(f"startup_check check=air_gapped_mode value={air_gapped}")
    return {
        "drift_orphans": drift,
        "env_vars": env,
        "air_gapped_mode": air_gapped,
        "all_strict_passed": drift["passed"],
        "any_warns": not env["passed"],
    }
