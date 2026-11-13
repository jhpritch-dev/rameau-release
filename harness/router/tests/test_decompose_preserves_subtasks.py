"""
Regression guard: decompose_node must never drop the decomposer's subtasks.

The v107a "N<=3 collapse" gate returned {"status": "DIRECT_ROUTE", "_gate": ...} on the
success path. Node returns are PARTIAL state updates, so omitting "subtasks" left the
state's subtasks == {}. fan_out then had nothing to dispatch and the graph hung forever
with elapsed_seconds frozen -- observed live on graphs 9df4f583 (v215) and 249f2b4a (v216).

The gate was also structurally unreachable: `decompose` fans out only to {fan_out, failed},
so the "skip fan_out" it advertised could never occur. It was removed in v217.

These tests pin the invariant, not the implementation: any success-path return from
decompose_node carries subtasks through, and the graph wiring can't silently grow a
decompose->direct_route branch without someone updating this file.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "autopilot_graph.py"
TREE = ast.parse(SRC.read_text(encoding="utf-8"))


def _decompose_node():
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "decompose_node":
            return node
    pytest.fail("decompose_node not found in autopilot_graph.py")


def _returns(fn):
    return [r for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None]


def test_no_success_return_drops_subtasks():
    """A non-FAILED dict return from decompose_node must carry subtasks.

    FAILED returns are exempt: the graph routes them to `failed`, which never fans out.
    """
    src = SRC.read_text(encoding="utf-8")
    offenders = []
    for r in _returns(_decompose_node()):
        seg = ast.get_source_segment(src, r.value) or ""
        if not isinstance(r.value, ast.Dict):
            continue  # e.g. `return result` -- carries the decomposer payload
        if '"FAILED"' in seg or "'FAILED'" in seg:
            continue  # failure paths legitimately carry no subtasks
        if "subtasks" not in seg:
            offenders.append((r.lineno, " ".join(seg.split())[:90]))
    assert not offenders, (
        "decompose_node has a non-FAILED dict return that omits 'subtasks'. Node returns "
        "are PARTIAL state updates, so this silently leaves subtasks == {} and fan_out "
        "hangs with nothing to dispatch:\n"
        + "\n".join(f"  line {ln}: {s}" for ln, s in offenders)
    )


def test_the_n3_collapse_gate_stays_removed():
    """The gate advertised a decompose->T1B shortcut that the graph cannot express."""
    src = SRC.read_text(encoding="utf-8")
    fn_src = ast.get_source_segment(src, _decompose_node()) or ""
    assert "DIRECT_ROUTE" not in fn_src, (
        "decompose_node returns DIRECT_ROUTE again. `decompose` fans out only to "
        "{fan_out, failed} -- DIRECT_ROUTE cannot reach direct_route from here, so it "
        "just drops subtasks and wedges the graph. Wire a real decompose->direct_route "
        "edge (and teach direct_route_node to accept subtasks) before reintroducing it."
    )


def test_decompose_edges_still_only_fan_out_or_failed():
    """Pins the wiring the two tests above depend on."""
    src = SRC.read_text(encoding="utf-8")
    # Match "decompose" as the SOURCE node, not merely a destination in some other
    # node's mapping (the `gate` edge maps to "decompose" and would false-match).
    edge = [
        ln for ln in src.splitlines()
        if 'add_conditional_edges("decompose"' in ln.replace(" ", "")
        or 'add_conditional_edges("decompose",' in ln
    ]
    assert edge, "decompose conditional edges not found -- graph wiring changed"
    line = edge[0]
    assert "direct_route" not in line, (
        "decompose now has a direct_route branch. If that is intentional, "
        "test_the_n3_collapse_gate_stays_removed needs revisiting."
    )
    assert '"fan_out"' in line and '"failed"' in line
