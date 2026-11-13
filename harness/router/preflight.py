#!/usr/bin/env python3
"""Pre-build gates for the bifrost-router image. Run BEFORE `docker build`.

Why this exists
---------------
Two config-of-record failures have each cost a deploy:

  * v205 crashlooped because fleet_config.json listed tiers (3-glm52, 3-nemotron-ultra)
    that had no member in config.py's Tier enum. `import main` does NOT catch this --
    the tier lookup happens in lifespan, which import never runs. Gate 1.

  * config.py has no fallback backend map since 2026-08-22 (PACKET-85 option B). It
    named 13 of 35 tiers and drifted silently -- on 2026-07-10 it pointed tier 1b at
    `bifrost-t1b`, which is gemma3 12.2B not granite 8.8B, and 1a-hearth at `qwen3:8b`,
    the thinking-trap model Hearth was migrated off. Both models exist on their hosts,
    so it failed SILENTLY. Deleting it removed the drift and the second source with it:
    a tier the enum knows about and fleet_config does not now has NO backend at all,
    and the first request routing to it raises KeyError in main.py. Gate 2 catches
    that at build time instead.

config_audit.py (in the autoagent repo) DETECTED the old fallback drift daily and exited
Nothing ENFORCED it, so an image could still be built with a poisoned DR fallback. This
script is the enforcer, and it is deliberately self-contained: no autoagent import, so it
runs from a clean checkout of this repo alone.

Usage
-----
    python preflight.py                       # gate the current working tree
    python preflight.py --config-path X.py    # gate a candidate config.py (e.g. a POSTIMAGE)
    python preflight.py --force "<reason>"    # bypass, loudly, with a recorded reason

Exit 0 = all gates pass. Exit 1 = a gate failed (do not build). Exit 2 = the gate itself
could not run (missing file, unparseable) -- also do not build, but the distinction matters:
a gate that cannot check must not silently report "pass".
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_config(path: Path):
    """Import a config.py by absolute path, with this repo on sys.path for its imports.

    Uses an explicit SourceFileLoader: spec_from_file_location() infers the loader from the
    file SUFFIX, so it cannot load an orch_apply POSTIMAGE (`config.py.POSTIMAGE-<ceremony>`,
    no .py extension). Gating a candidate image before signing it is the main reason
    --config-path exists, so this must work on arbitrary filenames.
    """
    if not path.is_file():
        raise FileNotFoundError(path)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    loader = importlib.machinery.SourceFileLoader("_preflight_config", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_fleet(path: Path) -> dict[str, str]:
    """{tier_id: active_model} from fleet_config.json."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {t["tier"]: t.get("active_model") for t in data["tiers"]}


def gate_tier_enum(cfg, fleet: dict[str, str]) -> list[str]:
    """fleet_config tiers must all exist in config.py's Tier enum.

    `import main` does not run lifespan, so a missing member surfaces only as a crashloop
    at deploy time. This is the v205 gate.
    """
    members = {t.value for t in cfg.Tier}
    orphans = sorted(set(fleet) - members)
    return [f"fleet_config tier {t!r} has no member in config.Tier (would crashloop, cf. v205)"
            for t in orphans]


def gate_fleet_covers_enum(cfg, fleet: dict[str, str]) -> list[str]:
    """Every Tier must have a backend in fleet_config.

    Since PACKET-85 option B there is no second source. A tier the enum knows about and
    fleet_config does not is a tier with no backend at all, and the first request that
    routes to it raises KeyError in main.py:922. Catch it at build time instead.
    """
    missing = sorted({t.value for t in cfg.Tier} - set(fleet))
    return [f"tier {t!r} is in the Tier enum but absent from fleet_config.json -- "
            f"there is no fallback backend map since 2026-08-22" for t in missing]


GATES = [
    ("tier-enum-subset", gate_tier_enum),
    ("fleet-covers-enum", gate_fleet_covers_enum),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="bifrost-router pre-build gates")
    ap.add_argument("--config-path", default=str(HERE / "config.py"))
    ap.add_argument("--fleet-config", default=str(HERE / "fleet_config.json"))
    ap.add_argument("--force", metavar="REASON",
                    help="bypass a FAILING gate; requires an explicit reason, printed loudly")
    args = ap.parse_args()

    cfg_path, fleet_path = Path(args.config_path), Path(args.fleet_config)
    try:
        cfg = _load_config(cfg_path)
        fleet = _load_fleet(fleet_path)
    except Exception as exc:  # noqa: BLE001 -- a gate that cannot run must not report "pass"
        sys.stdout.flush()
        print(f"PREFLIGHT COULD NOT RUN: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Refusing to build. A gate that cannot check is not a gate that passed.",
              file=sys.stderr)
        return 2

    print(f"preflight: config={cfg_path.name}  fleet_config={fleet_path.name}  "
          f"tiers={len(fleet)}")
    failures: list[str] = []
    for name, fn in GATES:
        errs = fn(cfg, fleet)
        print(f"  [{'FAIL' if errs else 'PASS'}] {name}")
        for e in errs:
            print(f"         {e}")
        failures.extend(errs)

    if not failures:
        print(f"preflight: {len(GATES)}/{len(GATES)} gates PASS -- safe to build")
        return 0

    if args.force:
        print(f"\npreflight: {len(failures)} failure(s) BYPASSED via --force")
        print(f"  reason: {args.force}")
        print("  Building a router image with a known-bad config of record. This is recorded.")
        return 0

    print(f"\npreflight: {len(failures)} failure(s) -- BUILD BLOCKED", file=sys.stderr)
    print("  Fix the config, or re-run with --force \"<reason>\" if you accept the risk.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
