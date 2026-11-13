"""policy_instructions._self_check had FAILED since 4cc383c (2026-06-26), and nothing ran it.

4cc383c appended a delegate note after the DO-NOT-USE list ("Delegation via delegate_subtask is
depth- and width-bounded ..."). The self-check substring-tested everything after "Do not call:",
so it reported the ALLOWED tool delegate_subtask as off limits. The policy and the model-facing
text were right all along; only the check was wrong. This file puts the check under pytest so it
cannot rot silently again, and pins the real invariant for every role.
"""
import pytest

import policy_instructions as pi

ROLES = ("igor", "leaf", "unknown-role")


def _listed(block):
    if "Do not call:" not in block:
        return set()
    text = block.split("Do not call:", 1)[1].split(". Any attempt", 1)[0]
    return {n.strip() for n in text.split(",") if n.strip()}


def test_self_check_passes():
    pi._self_check()


@pytest.mark.parametrize("role", ROLES)
def test_offlimits_list_matches_the_policy(role):
    pol = pi.load_policy()
    listed = _listed(pi.instruction_block(role, pol))
    allowed = pi.allowed_tools(role, pol)
    assert not (listed & allowed), f"allowed tools listed as off limits: {sorted(listed & allowed)}"
    assert listed == pi.denied_tools(role, pol) | (pi.allowed_tools("igor", pol) - allowed)


def test_self_check_still_catches_an_allowed_tool_in_the_list(monkeypatch):
    pol = pi.load_policy()
    victim = sorted(pi.allowed_tools("leaf", pol))[0]
    bad = ("These tools are OFF LIMITS. Do not call: "
           + ", ".join(sorted(pi.denied_tools("leaf", pol)) + [victim])
           + ". Any attempt is logged and refused.")
    monkeypatch.setattr(pi, "instruction_block", lambda role="igor", policy=None: bad)
    with pytest.raises(AssertionError, match=victim):
        pi._self_check()


def test_self_check_still_catches_a_missing_denied_tool(monkeypatch):
    pol = pi.load_policy()
    denied = sorted(pi.denied_tools("leaf", pol))
    bad = "These tools are OFF LIMITS. Do not call: " + ", ".join(denied[1:]) + ". Any attempt is logged and refused."
    monkeypatch.setattr(pi, "instruction_block", lambda role="igor", policy=None: bad)
    with pytest.raises(AssertionError, match=denied[0]):
        pi._self_check()
