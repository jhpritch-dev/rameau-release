"""
bifrost_warmup.py -- BIFROST Fleet Warmup
==========================================
Pre-loads all active tier models into VRAM so the first
AUTOPILOT subtask doesn't time out waiting for a cold load.

Run from Bifrost before any AUTOPILOT session:
    python bifrost_warmup.py

Or target specific tiers:
    python bifrost_warmup.py --tiers 1a-hearth 1a-coder 2

Skips cloud tiers (3-claude, 3-gemini, 3-fast) and NPU (flm manages its own load).
Skips tiers that are already loaded.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

FLEET_CONFIG = Path(r"D:\Projects\bifrost-router\fleet_config.json")
KEEP_ALIVE = "30m"
TIMEOUT = 120  # seconds per model load

SKIP_TYPES = {"anthropic"}         # cloud tiers ? never pre-warm
SKIP_LABELS = {"cloud"}              # skip display label for cloud machine tiers
SKIP_TIERS = {"npu", "3-claude", "3-gemini", "3-fast", "search"}  # search=Session F only, skip warmup to preserve T1B VRAM  # FLM self-manages, cloud skipped


def load_fleet() -> list[dict]:
    with open(FLEET_CONFIG, encoding="utf-8-sig") as f:
        return json.load(f)["tiers"]


def get_loaded_models(base_url: str) -> set[str]:
    """Return set of currently loaded model names on an Ollama instance."""
    try:
        req = urllib.request.Request(f"{base_url}/api/ps")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return {m["model"] for m in data.get("models", [])}
    except Exception:
        return set()


def warm_model(base_url: str, model: str, tier_label: str) -> bool:
    """Send a keep-alive load request. Returns True on success."""
    payload = json.dumps({
        "model": model,
        "prompt": "",
        "keep_alive": KEEP_ALIVE,
    }).encode()

    try:
        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True
    except urllib.error.URLError as e:
        print(f"  ✗ {tier_label}: connection failed — {e.reason}")
        return False
    except Exception as e:
        print(f"  ✗ {tier_label}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="BIFROST Fleet Warmup")
    parser.add_argument("--tiers", nargs="*", help="Specific tiers to warm (default: all local)")
    parser.add_argument("--force", action="store_true", help="Reload even if already loaded")
    args = parser.parse_args()

    tiers = load_fleet()
    target_tiers = set(args.tiers) if args.tiers else None

    print("BIFROST Fleet Warmup")
    print("=" * 40)

    # Group by base_url to check loaded models once per Ollama instance
    seen_instances: dict[str, set[str]] = {}
    ok = 0
    skipped = 0
    failed = 0

    for t in tiers:
        tier_id = t["tier"]
        label = t["label"]
        model = t["active_model"]
        base_url = t.get("base_url", "")
        tier_type = t.get("type", "ollama")

        # Filter
        if target_tiers and tier_id not in target_tiers:
            continue
        if tier_id in SKIP_TIERS or tier_type in SKIP_TIERS:
            print(f"  — {label}: skipped (cloud/NPU)")
            skipped += 1
            continue
        if tier_type != "ollama":
            print(f"  — {label}: skipped (non-ollama type={tier_type})")
            skipped += 1
            continue
        if not base_url:
            print(f"  — {label}: skipped (no base_url)")
            skipped += 1
            continue

        # Check if already loaded
        if not args.force:
            if base_url not in seen_instances:
                seen_instances[base_url] = get_loaded_models(base_url)
            loaded = seen_instances[base_url]
            if model in loaded or f"{model}:latest" in loaded:
                print(f"  ✓ {label}: already loaded ({model})")
                ok += 1
                continue

        print(f"  ↑ {label}: loading {model} ...", end="", flush=True)
        success = warm_model(base_url, model, label)
        if success:
            print(" done")
            if base_url in seen_instances:
                seen_instances[base_url].add(model)
            ok += 1
        else:
            failed += 1

    print()
    print(f"Done — {ok} ready, {skipped} skipped, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
