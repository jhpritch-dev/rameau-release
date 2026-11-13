"""Adoption C: gate loading, validation, mtime reload, and overlay tightening.

The security-relevant property is the last one: an overlay must not be able to
switch off the control it exists to enforce.
"""
import os
import time

import pytest

import bifrost_gates as G

BASE = """
schema_version = 1

[tool_classes]
READ    = { enabled = true, max_risk = 0.3, ceremony_required = false, review_required = false }
EXECUTE = { enabled = true, max_risk = 0.8, ceremony_required = true,  review_required = true  }

[phase_gates.review_to_done]
human_approval_required = false
shieldstral_score_max = 0.70
"""


def write(p, text):
    p.write_text(text, encoding="utf-8")
    return str(p)


# ------------------------------------------------------------------ loading

def test_the_shipped_file_loads_and_validates():
    """The real bifrost-gates.toml, not a fixture -- a fixture that passes while
    the shipped file is broken proves nothing."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(G.__file__)))
    path = os.path.join(os.path.dirname(os.path.abspath(G.__file__)),
                        "bifrost-gates.toml")
    if not os.path.exists(path):
        path = os.path.join(here, "bifrost-gates.toml")
    doc = G.load_gates(path)
    assert doc["schema_version"] == 1
    assert doc["tool_classes"]["EXECUTE"]["ceremony_required"] is True


def test_the_class_set_matches_the_live_policy():
    """The gates file must describe the classes that actually exist.

    The brief listed five (READ/WRITE/EXECUTE/DELEGATE/EXTERNAL). The live surface
    has eight in bifrost_shell_policy.json and has no EXTERNAL at all, so a gates
    file built from the brief would have gated four classes that do not exist and
    left four real ones ungated. This fails the day the two drift apart.
    """
    repo = os.path.dirname(os.path.abspath(G.__file__))
    policy_path = os.path.join(repo, "bifrost_shell_policy.json")
    if not os.path.exists(policy_path):
        pytest.skip("bifrost_shell_policy.json not in this checkout")
    import json
    live = set(json.load(open(policy_path, encoding="utf-8")).get("classes", {}))
    gates = set(G.load_gates(os.path.join(repo, "bifrost-gates.toml"))["tool_classes"])
    assert gates == live, (
        "gates classes %s do not match the live policy %s"
        % (sorted(gates - live), sorted(live - gates)))


def test_an_overlay_may_disable_a_class_but_not_enable_one():
    """Turning a capability OFF is tightening; turning one back ON is not."""
    base = {"tool_classes": {"EXECUTE": {"enabled": True},
                             "SANDBOX": {"enabled": False}}}
    merged, _ = G.merge_overlay(base, {"tool_classes": {"EXECUTE": {"enabled": False}}})
    assert merged["tool_classes"]["EXECUTE"]["enabled"] is False
    with pytest.raises(G.GateError) as e:
        G.merge_overlay(base, {"tool_classes": {"SANDBOX": {"enabled": True}}})
    assert "may not loosen" in str(e.value)


def test_a_parse_error_and_a_schema_error_raise_the_same_type(tmp_path):
    """One thing for a caller to catch."""
    with pytest.raises(G.GateError):
        G.load_gates(write(tmp_path / "bad.toml", "this is not [ toml"))
    with pytest.raises(G.GateError):
        G.load_gates(write(tmp_path / "v.toml", "schema_version = 99\n"))
    with pytest.raises(G.GateError):
        G.load_gates(str(tmp_path / "does-not-exist.toml"))


def test_a_true_is_not_accepted_as_a_risk_number(tmp_path):
    """bool subclasses int, so a naive isinstance check lets max_risk = true through."""
    bad = BASE.replace("max_risk = 0.3", "max_risk = true")
    with pytest.raises(G.GateError) as e:
        G.load_gates(write(tmp_path / "b.toml", bad))
    assert "must be a number" in str(e.value)


def test_risk_out_of_range_is_refused(tmp_path):
    bad = BASE.replace("max_risk = 0.3", "max_risk = 4.2")
    with pytest.raises(G.GateError) as e:
        G.load_gates(write(tmp_path / "b.toml", bad))
    assert "out of range" in str(e.value)


def test_a_missing_key_names_the_class_and_the_key(tmp_path):
    bad = BASE.replace(", review_required = false }", " }", 1)
    with pytest.raises(G.GateError) as e:
        G.load_gates(write(tmp_path / "b.toml", bad))
    assert "READ" in str(e.value) and "review_required" in str(e.value)


# ------------------------------------------------------------------- reload

def test_reload_happens_on_mtime_change_not_on_every_call(tmp_path):
    p = tmp_path / "g.toml"
    store = G.GateStore(write(p, BASE))
    assert store.tool_class("READ")["max_risk"] == 0.3
    for _ in range(5):
        store.gates()
    assert store.reload_count == 1, "re-parsed an unchanged file"

    time.sleep(0.01)
    write(p, BASE.replace("max_risk = 0.3", "max_risk = 0.25"))
    assert store.tool_class("READ")["max_risk"] == 0.25
    assert store.reload_count == 2


def test_a_broken_edit_keeps_the_last_good_config(tmp_path):
    """A hand-edited policy file must not be able to take the tool surface down,
    and must not silently degrade to 'no gates' either."""
    p = tmp_path / "g.toml"
    store = G.GateStore(write(p, BASE))
    assert store.tool_class("EXECUTE")["ceremony_required"] is True

    time.sleep(0.01)
    write(p, "totally [ broken")
    assert store.tool_class("EXECUTE")["ceremony_required"] is True, \
        "a broken edit dropped the gates"
    assert store.last_error and "parse" in store.last_error

    time.sleep(0.01)
    write(p, BASE.replace("max_risk = 0.8", "max_risk = 0.5"))
    assert store.tool_class("EXECUTE")["max_risk"] == 0.5
    assert store.last_error is None


def test_a_broken_file_is_not_reparsed_every_call(tmp_path):
    p = tmp_path / "g.toml"
    store = G.GateStore(write(p, BASE))
    store.gates()          # establish a good config first; a FIRST load is meant to raise
    time.sleep(0.01)
    write(p, "broken [")
    store.gates()
    n = store.reload_count
    for _ in range(5):
        store.gates()
    assert store.reload_count == n


# ------------------------------------------------------------------ overlay

def test_an_overlay_may_tighten():
    base = {"tool_classes": {"EXECUTE": {"ceremony_required": True,
                                         "review_required": False,
                                         "max_risk": 0.8}}}
    over = {"tool_classes": {"EXECUTE": {"review_required": True,
                                         "max_risk": 0.5}}}
    merged, rejected = G.merge_overlay(base, over)
    assert merged["tool_classes"]["EXECUTE"]["review_required"] is True
    assert merged["tool_classes"]["EXECUTE"]["max_risk"] == 0.5
    assert rejected == []


def test_an_overlay_may_not_switch_a_control_off():
    """The briefed design merged freely, so a client overlay could set
    ceremony_required = false and disable the thing it exists to enforce."""
    base = {"tool_classes": {"EXECUTE": {"ceremony_required": True}}}
    over = {"tool_classes": {"EXECUTE": {"ceremony_required": False}}}
    with pytest.raises(G.GateError) as e:
        G.merge_overlay(base, over)
    assert "may not loosen" in str(e.value)
    assert "ceremony_required" in str(e.value)


def test_an_overlay_may_not_raise_the_risk_ceiling():
    base = {"tool_classes": {"READ": {"max_risk": 0.3}}}
    with pytest.raises(G.GateError):
        G.merge_overlay(base, {"tool_classes": {"READ": {"max_risk": 0.95}}})


def test_non_strict_merge_drops_and_reports_rather_than_raising():
    """A client's bad overlay should be ignored and logged, not fatal."""
    base = {"tool_classes": {"EXECUTE": {"ceremony_required": True, "max_risk": 0.8}}}
    over = {"tool_classes": {"EXECUTE": {"ceremony_required": False, "max_risk": 0.4}}}
    merged, rejected = G.merge_overlay(base, over, strict=False)
    assert merged["tool_classes"]["EXECUTE"]["ceremony_required"] is True
    assert merged["tool_classes"]["EXECUTE"]["max_risk"] == 0.4
    assert rejected == [("tool_classes.EXECUTE.ceremony_required", True, False)]


def test_operational_keys_are_not_constrained():
    base = {"phase_gates": {"pending_to_running": {"max_concurrent_running": 3}}}
    over = {"phase_gates": {"pending_to_running": {"max_concurrent_running": 8}}}
    merged, rejected = G.merge_overlay(base, over)
    assert merged["phase_gates"]["pending_to_running"]["max_concurrent_running"] == 8
    assert rejected == []


# -------------------------------------------------------------------- inert

def test_the_module_installs_no_signal_handler():
    """The briefed design called signal.signal(signal.SIGHUP, ...) at import --
    AttributeError on Windows, which is where bifrost_shell.py runs.

    Checked by parsing, not by searching the text: the docstring above quotes the
    briefed call in order to explain it, and a substring guard cannot tell that
    from a real one. Three guards I wrote today tripped on their own prose.
    """
    import ast

    tree = ast.parse(open(G.__file__, encoding="utf-8").read())
    imported, calls = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                calls.append("%s.%s" % (fn.value.id, fn.attr))
    assert "signal" not in imported, "module imports signal"
    assert "signal.signal" not in calls, "module installs a signal handler"


def test_the_wiring_into_the_tool_surface_is_guarded():
    """Ceremony 3 (2026-09-21) wired this module into bifrost_shell.py.

    Until then this test asserted NOTHING imported it -- rule 2's inertness -- and
    it duly failed the moment the ceremony applied the wiring, which is what it was
    for. The rule-3 event has now happened under a ceremony, so the property worth
    guarding changed: bifrost_shell.py is the MCP tool surface, and an unguarded
    import there means a missing or unparsable gates file takes every tool down.

    Checked by parsing, so a mention in a comment or docstring cannot satisfy it.
    """
    import ast

    repo = os.path.dirname(os.path.abspath(G.__file__))
    shell = os.path.join(repo, "bifrost_shell.py")
    assert os.path.exists(shell), \
        "bifrost_shell.py not found at %s -- this guard must not silently skip" % shell

    tree = ast.parse(open(shell, encoding="utf-8", errors="replace").read())

    def imports_gates(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Import) and any(a.name == "bifrost_gates" for a in n.names):
                return True
            if isinstance(n, ast.ImportFrom) and n.module == "bifrost_gates":
                return True
        return False

    assert imports_gates(tree), "bifrost_shell.py no longer imports bifrost_gates"
    guarded = any(imports_gates(n) for n in ast.walk(tree) if isinstance(n, ast.Try))
    assert guarded, (
        "bifrost_shell.py imports bifrost_gates OUTSIDE a try -- an unparsable gates "
        "file or an interpreter without tomllib would take the whole tool surface down")
