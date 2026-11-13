"""
Guards the security property: the router must not execute model-generated code.

WHY. _verify_level5 ran `subprocess.run(["python", "-m", "pytest", <tempfile>])` on
untrusted model output INSIDE the router pod -- a pod with cluster network reach and the
router's own service account. The AIO Sandbox exists precisely to run that code somewhere
safe, and it works: probed 2026-07-16 from inside the router pod, execsvc is non-root,
resource-capped and egress-blocked (outbound connect -> ConnectionRefusedError).

WHAT DISABLING COSTS: nothing. L5 has never gated anything. `passed = returncode in (0, 5)`
scores exit 5 -- "no tests collected" -- as a PASS, and every code leg to date emitted an
implementation with no tests. The gate has been a rubber stamp since it was written
(TODO A18 / BUG-2), so turning the execution off removes a real risk and loses no signal.

NOT YET TRUE, deliberately: the sandbox is not the replacement gate yet. It only runs the
module top-level and never invokes pytest, so it CANNOT fail a bad test (probed: an
assertion-failing test_f() returns passed=true, because the function is merely defined and
never called). Wiring it in as-is would swap one rubber stamp for another. execsvc must
learn to run tests first -- see BRIEF-sandbox-inversion. Until then L5 is honestly inert
rather than dishonestly green.
"""
import subprocess

import pytest

from subtask_graph import _verify_level5

CODE_FAILING_TEST = (
    "```python\n"
    "def f():\n"
    "    return 2\n"
    "\n"
    "def test_f():\n"
    "    assert f() == 1\n"
    "```"
)


class _Spec:
    subtask_id = "s1"
    description = "implement f"


def test_disabled_by_default(monkeypatch):
    """No env var -> no execution. This is the security property."""
    monkeypatch.delenv("BIFROST_LOCAL_PYTEST", raising=False)
    r = _verify_level5(CODE_FAILING_TEST, _Spec())
    assert r["ran"] is False
    assert "disabled" in r["summary"]


def test_disabled_does_not_spawn_a_subprocess(monkeypatch):
    """The real assertion: prove no process is created, rather than trusting the flag."""
    monkeypatch.delenv("BIFROST_LOCAL_PYTEST", raising=False)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run called -- untrusted code executed in the router")

    monkeypatch.setattr(subprocess, "run", _boom)
    _verify_level5(CODE_FAILING_TEST, _Spec())  # must not raise


@pytest.mark.parametrize("val", ["0", "", "true", "yes", "1 "])
def test_only_exactly_1_enables_it(monkeypatch, val):
    """Fail closed: anything other than exactly "1" leaves execution off."""
    monkeypatch.setenv("BIFROST_LOCAL_PYTEST", val)
    assert _verify_level5(CODE_FAILING_TEST, _Spec())["ran"] is False


def test_escape_hatch_still_works(monkeypatch):
    """The fallback must remain usable for local dev, or it is not a fallback."""
    monkeypatch.setenv("BIFROST_LOCAL_PYTEST", "1")
    r = _verify_level5(CODE_FAILING_TEST, _Spec())
    assert r["ran"] is True
    # and it is a REAL gate when on: pytest collects test_f and it fails
    assert r["passed"] is False


def test_disabled_verdict_does_not_fail_the_leg(monkeypatch):
    """ran=False must carry passed=True, or disabling the gate fails every code leg."""
    monkeypatch.delenv("BIFROST_LOCAL_PYTEST", raising=False)
    assert _verify_level5(CODE_FAILING_TEST, _Spec())["passed"] is True


def test_the_deployment_does_not_set_the_escape_hatch():
    """Setting BIFROST_LOCAL_PYTEST=1 in k3d would reinstate the vulnerability."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for pat in ("*.yaml", "*.yml"):
        for f in list(root.glob(pat)) + list(root.glob("k8s/" + pat)) + list(root.glob("deploy/" + pat)):
            assert "BIFROST_LOCAL_PYTEST" not in f.read_text(encoding="utf-8", errors="ignore"), (
                f"{f} sets BIFROST_LOCAL_PYTEST -- that re-enables in-router execution"
            )
