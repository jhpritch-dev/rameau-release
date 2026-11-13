"""Gate-docs drift (PENDING-router-igor-gate-docs-drift-2026-09-11).

igor_tool_loop.py carried a module constant ENABLED_CLASSES = {READ, ANALYZE, DELEGATE}
that no code read and that disagreed with the live gate (igor and leaf are also granted
SANDBOX and READ_FS). Both igor_tool_loop.py and policy_instructions.py also stated tool
counts (14/14/13 and 15/15/9) that no longer matched allowed_tools(role).

The first two tests fail on the unmodified files. The rest pin the live gate from the
policy (they pass before and after) so the corrected comments stay true.
"""
import os
import re

import pytest

import igor_tool_loop as itl
import policy_instructions as pi

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The count-claim shapes the stale comments used: "-> 14", "gate=14", "(14)", "(13, ...",
# "(9 as of", "(14, unchanged)", "14 enabled-class tools", "15 tools", "14/14/12", "'13'".
_COUNT_CLAIM = re.compile(
    r"(?:->|=)\s*1[0-9]\b"
    r"|\((?:[5-9]|1[0-9])(?:\)|,| as of| unchanged)"
    r"|\b1[0-9] (?:enabled-class )?tools\b"
    r"|\b1[0-9]/1[0-9]/1?[0-9]\b"
    r"|'1[0-9]'"
)
_FS_READERS = {"read_file", "grep_repo", "list_dir", "git_read"}


def _src(name):
    with open(os.path.join(_ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def _policy():
    return pi.load_policy(os.path.join(_ROOT, "bifrost_shell_policy.json"))


def test_modules_come_from_this_tree():
    # A scratch suite once imported the REAL repo through a stray sys.path entry.
    for mod in (itl, pi):
        assert os.path.dirname(os.path.abspath(mod.__file__)) == _ROOT, mod.__file__


def test_no_dead_enabled_classes_constant():
    assert not hasattr(itl, "ENABLED_CLASSES"), (
        "igor_tool_loop.ENABLED_CLASSES is back; no code reads it and it is not the gate "
        "(the gate is policy_instructions.allowed_tools(role))")
    assert not re.search(r"(?m)^\s*ENABLED_CLASSES\s*=", _src("igor_tool_loop.py"))


@pytest.mark.parametrize("name", ["igor_tool_loop.py", "policy_instructions.py"])
def test_no_hardcoded_tool_count_claims(name):
    hits = [(i, line.strip()) for i, line in enumerate(_src(name).splitlines(), 1)
            if _COUNT_CLAIM.search(line)]
    assert hits == [], "tool-count claims in %s (compute len(allowed_tools(role))): %r" % (name, hits)


def test_gate_is_exactly_the_policy_allowed_set():
    pol = _policy()
    assert itl.SAFE_TOOL_NAMES == frozenset(pi.allowed_tools("igor", pol)) - itl.ADVERTISED_EXCLUDE
    assert itl._safe_for_role("leaf") == frozenset(pi.allowed_tools("leaf", pol)) - itl.ADVERTISED_EXCLUDE


def test_live_gate_is_wider_than_the_removed_constant():
    # What the dead constant got wrong: igor and leaf also hold SANDBOX and READ_FS tools.
    pol = _policy()
    for role in ("igor", "leaf"):
        gate = itl._safe_for_role(role)
        assert "run_in_sandbox" in gate
        assert _FS_READERS <= gate
        assert {"SANDBOX", "READ_FS"} <= {pi.tool_class(t, pol) for t in gate}


def test_unknown_role_fails_closed_to_read_and_analyze():
    pol = _policy()
    gate = itl._safe_for_role("no-such-role")
    assert gate
    assert {pi.tool_class(t, pol) for t in gate} <= {"READ", "ANALYZE"}
    assert "delegate_subtask" not in gate and "run_in_sandbox" not in gate
    assert not (_FS_READERS & gate)


def test_advertised_list_is_the_gate_minus_the_page_tool():
    # Backs the corrected SAFE_TOOL_NAMES comment: tool_output_page is in the gate (READ) but
    # joins the advertised list only after the loop pages a result.
    names = {d["function"]["name"] for d in itl.get_safe_tool_definitions("igor")}
    assert itl.PAGE_TOOL_NAME in itl.SAFE_TOOL_NAMES
    assert names == set(itl.SAFE_TOOL_NAMES) - {itl.PAGE_TOOL_NAME}
