"""Ceremony 3: the gates layer in bifrost_shell.py.

This is the MCP tool surface, so the two properties that matter are that the gate
can only ever DENY more than policy already denies, and that a broken gates file
cannot take the surface down.
"""
import importlib

import pytest

shell = importlib.import_module("bifrost_shell")


def _call(cls_name, fn=None):
    """Build a gated tool and invoke it, returning whatever the wrapper produced."""
    fn = fn or (lambda: {"ok": True})
    fn.__name__ = getattr(fn, "__name__", "probe")
    return shell.gated_tool(cls_name)(fn)()


# ---------------------------------------------------------------- additive only

def test_a_gate_cannot_grant_what_policy_denies(monkeypatch):
    """The whole safety argument. Policy off + gate on must still be denied."""
    monkeypatch.setattr(shell, "_POLICY", {"classes": {"EXECUTE": {"enabled": False}}})
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {"enabled": True, "max_risk": 1.0})})())
    out = _call("EXECUTE")
    assert out.get("policy_denied") is True, "gates granted what policy withheld"


def test_a_gate_can_deny_what_policy_allows(monkeypatch):
    """The point of the feature: a deployment may tighten."""
    monkeypatch.setattr(shell, "_POLICY", {"classes": {"WRITE": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {"enabled": False})})())
    out = _call("WRITE")
    assert out.get("policy_denied") is True


def test_policy_allow_plus_gate_allow_runs(monkeypatch):
    monkeypatch.setattr(shell, "_POLICY", {"classes": {"READ": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {"enabled": True})})())
    assert _call("READ") == {"ok": True}


# ------------------------------------------------- cannot break the tool surface

def test_no_gates_at_all_leaves_behaviour_unchanged(monkeypatch):
    monkeypatch.setattr(shell, "_POLICY", {"classes": {"READ": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", None)
    assert _call("READ") == {"ok": True}
    assert shell._gate_denies("READ") is False


def test_a_raising_gate_store_does_not_deny_everything(monkeypatch):
    """A broken gates file must not read as 'disable the whole surface'."""
    class Boom:
        @staticmethod
        def tool_class(name):
            raise RuntimeError("unparsable")

    monkeypatch.setattr(shell, "_POLICY", {"classes": {"READ": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", Boom())
    assert shell._gate_denies("READ") is False
    assert _call("READ") == {"ok": True}


def test_a_class_absent_from_the_gates_file_is_not_denied(monkeypatch):
    """Absence is not a deny -- only an explicit enabled=false is."""
    monkeypatch.setattr(shell, "_POLICY", {"classes": {"READ": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {})})())
    assert shell._gate_denies("READ") is False


def test_only_an_explicit_false_denies(monkeypatch):
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {"enabled": None})})())
    assert shell._gate_denies("READ") is False


# ------------------------------------------------------------------ async path

def test_the_async_wrapper_gates_too(monkeypatch):
    """Both wrappers exist; a gate applied to only one is a hole."""
    import asyncio

    monkeypatch.setattr(shell, "_POLICY", {"classes": {"WRITE": {"enabled": True}}})
    monkeypatch.setattr(shell, "_GATES", type("G", (), {
        "tool_class": staticmethod(lambda n: {"enabled": False})})())

    async def probe():
        return {"ok": True}

    wrapped = shell.gated_tool("WRITE")(probe)
    out = asyncio.run(wrapped())
    assert out.get("policy_denied") is True, "async path bypassed the gate"


# ------------------------------------------------------------------ wiring shape

def test_the_gate_is_consulted_after_policy_not_before():
    """Order is the safety argument; assert it in the source, by parsing."""
    import ast
    import inspect

    src = inspect.getsource(shell.gated_tool)
    tree = ast.parse(src.strip())
    text = ast.dump(tree)
    # _POLICY must appear before _gate_denies in both wrapper bodies.
    for body in src.split("def ")[1:]:
        if "_gate_denies" not in body:
            continue
        assert body.index("_POLICY") < body.index("_gate_denies"), \
            "gate consulted before policy in %s" % body.splitlines()[0]
    assert text  # parsed


def test_the_real_gates_file_loads_through_the_store():
    """Not a fixture -- the shipped file, through the shipped store."""
    if shell._GATES is None:
        pytest.skip("gates store unavailable in this environment")
    g = shell._GATES.gates()
    assert g["schema_version"] == 1
    assert "EXECUTE" in g["tool_classes"]
    # and every live policy class is represented
    for name in shell._POLICY.get("classes", {}):
        assert name in g["tool_classes"], "class %s has no gate entry" % name
