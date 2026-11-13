"""Air-gap state for the modules main.py imports (CEREMONY 123, 2026-10-13).

main.py owns the air gap: _AIR_GAPPED (env BIFROST_AIR_GAPPED, or POST /airgap, latched ON) behind
_air_gapped(), which also counts both OFFLINE modes. main.py enforces it only where it composes a
cascade. strategies.py (the TWO_PASS cloud review) and subtask_graph.py (the AUTOPILOT cloud legs) reach
the cloud without passing there, and they cannot import main: main imports them.

air_gapped() calls main's own accessor at decision time, from the module main was loaded as (the pod
runs `uvicorn main:app`), so an API flip counts from the next decision. That is the contract
main.py:149-151 sets: every consumer calls _air_gapped(), never a captured copy.

When main is not loaded (a unit test, a script driving the graph), POST /airgap cannot exist, so main's
start-up rule is exact: the env var, or an OFFLINE mode. An error while reading the state answers True:
refusing a cloud call is the safe direction.
"""
import os
import sys

_MAIN_MODULES = ("main", "__main__")


def air_gapped() -> bool:
    try:
        for name in _MAIN_MODULES:
            accessor = getattr(sys.modules.get(name), "_air_gapped", None)
            if callable(accessor):
                return bool(accessor())
        if os.environ.get("BIFROST_AIR_GAPPED", "false").strip().lower() == "true":
            return True
        from config import OperatingMode, settings
        return settings.current_mode in (OperatingMode.JARVIS_OFFLINE, OperatingMode.WORKSHOP_OFFLINE)
    except Exception:
        return True
