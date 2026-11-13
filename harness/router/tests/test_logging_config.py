"""main.py's logging config must take effect even when an earlier import has logged to the root logger.

2026-09-10: in the k3d pod the hostname maps to no fleet machine, so telemetry.py's import-time
logging.warning() ran an implicit basicConfig() before main.py's own call, which then did nothing:
root stayed at WARNING and every bifrost.router INFO line was dropped. The probe runs in a
subprocess because this process's root logger already carries pytest's handlers, which hide the bug.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_PROBE = r'''
import logging, socket
socket.gethostname = lambda: "bifrost-router-6c5b964d86-zxslb"   # a pod name: maps to no fleet machine
import main
root = logging.getLogger()
print("ROOT", logging.getLevelName(root.level))
print("INFO", logging.getLogger("bifrost.router").isEnabledFor(logging.INFO))
for h in root.handlers:
    print("FMT", getattr(h.formatter, "_fmt", None))
print("HTTPX", logging.getLogger("httpx").getEffectiveLevel())
'''


def test_router_info_logging_survives_an_import_time_root_warning(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "BIFROST_MACHINE"}
    env.update(LOG_LEVEL="INFO", BIFROST_TELEMETRY_DIR=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    # THE CHILD NEEDS THE REPOSITORY ROOT ON ITS PATH, and sys.path does not cross a process
    # boundary -- so the root conftest.py fix that makes `shared` importable in THIS process
    # does nothing for the probe below. The probe imports main, and main.py imports
    # shared.resolver. In the image the two sit in one directory and the question does not
    # arise; in the source layout `shared/` is two levels up from ROOT.
    _repo_root = str(ROOT.parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (_repo_root, str(ROOT), env.get("PYTHONPATH", "")) if p])
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout.splitlines()
    # the trigger must still fire, or this test proves nothing
    assert "[telemetry] hostname" in r.stderr, "telemetry no longer warns on a pod hostname: re-aim this test"
    assert "ROOT INFO" in out, r.stdout
    assert "INFO True" in out, r.stdout
    assert f"FMT {MAIN_FMT}" in out, r.stdout
    assert "HTTPX 30" in out, r.stdout
