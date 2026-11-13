"""policy_instructions.py -- single-source policy -> {advertised subset, DO-NOT-USE
block, gate drift guard}.

Ceremony 1 (igor-delegate-route-ceremony1, 2026-06-27). The anti-drift spine:
bifrost_shell_policy.json is the ONE source of tool->class truth. This module
derives, from that single source:
  - allowed_tools(role)     -- tools whose class is ENABLED in the policy AND granted to the role
  - denied_tools(role)      -- tools in DISABLED classes  (WRITE + EXECUTE)  == OFF LIMITS
  - instruction_block(role) -- generated DO-NOT-USE text naming exactly the denied tools
  - assert_no_drift(...)    -- tiered gate-vs-policy guard (see below)

Pure, deterministic, ASCII. No I/O beyond reading the policy JSON. No network.

Ceremony 3 (igor-dynamic-tools-ceremony3, 2026-06-27): the gate (SAFE_TOOL_NAMES
in igor_tool_loop.py) is now POLICY-DERIVED -- it IS the set of policy enabled-
class tools (minus an ADVERTISED_EXCLUDE that is empty under Option i). So the
drift guard is no longer a guard against a hand-authored set silently diverging;
it is a runtime CONFIRMATION of two structural properties:
  HARD (raise): gate & denied == set()    -- no OFF-LIMITS tool reachable.
  HARD (raise): gate - allowed == set()   -- every gate tool is policy-allowed
      (enabled class); an UNCLASSIFIED tool defaults out (fail-closed).
The Stack-B carve-out (STACKB_KNOWN) is RETIRED: C2 classified grep_repo/
list_dir/git_read as READ -- RECLASSED to READ_FS on 2026-09-09 (Rule-1
Option A, signed John) -- so there is no longer any policy-unclassified gate
tool. The SOFT warn-set is therefore EMPTY by construction -- that empty set is
the C2+C3 acceptance signal.
"""
from __future__ import annotations

import json
import os
from typing import Iterable

# Ceremony 3 (2026-06-27): STACKB_KNOWN RETIRED. C2 classified grep_repo/list_dir/
# git_read as READ -- RECLASSED to READ_FS on 2026-09-09 (Rule-1 Option A, signed
# John); they are still CLASSIFIED, so no gate tool is policy-unclassified anymore
# and the gate is still policy-derived.
#
# 2026-09-09: the four host-FS readers (read_file, grep_repo, list_dir, git_read)
# are now ALL class READ_FS, and READ_FS is granted to igor and leaf below. Setting
# READ_FS.enabled=false in bifrost_shell_policy.json removes all four from every
# role (demonstrated, not assumed).
#
# STACKB_KNOWN is kept as an empty frozenset for backward-compat of any external
# import; assert_no_drift no longer special-cases Stack-B (rogue == gate - allowed).
STACKB_KNOWN = frozenset()

_DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bifrost_shell_policy.json")


class PolicyDriftError(RuntimeError):
    """Raised when the live gate set violates a HARD policy safety invariant
    (a denied tool is reachable, or a rogue tool is in the gate)."""


def load_policy(path: str | None = None) -> dict:
    """Read bifrost_shell_policy.json (the ONE source). Env override:
    BIFROST_SHELL_POLICY. Returns the parsed dict."""
    p = path or os.environ.get("BIFROST_SHELL_POLICY", _DEFAULT_POLICY_PATH)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _enabled_classes(policy: dict) -> set:
    return {c for c, v in policy.get("classes", {}).items() if v.get("enabled")}


def _disabled_classes(policy: dict) -> set:
    return {c for c, v in policy.get("classes", {}).items() if not v.get("enabled")}


# B2 (2026-06-27): per-caller role taxonomy. A role's allowed classes intersect the
# policy-enabled classes. igor keeps the full set (READ+ANALYZE+DELEGATE+SANDBOX+
# READ_FS); a leaf is Option B (John 2026-06-27): leaf = that same set (== igor;
# count it with len(allowed_tools(role)); it is not restated here). Leaf MAY
# delegate -> recursive leaf->leaf is ENABLED; B3/B4 MUST add the recursion bound
# (delegate_subtask is inert in B2: plain completion until B3 wires mode=tools).
# Unknown roles default to the conservative READ+ANALYZE set (fail-closed).
ROLE_ENABLED_CLASSES = {
    # READ_FS granted 2026-09-09 (Rule-1 Option A, signed John): READ_FS was created
    # 2026-08-04 and granted to NO role, which silently killed read_file for igor and
    # leaf; the same grant keeps grep_repo/list_dir/git_read working now that they are
    # READ_FS too. One flag (READ_FS.enabled) now disables all four host-FS readers.
    "igor": frozenset({"READ", "ANALYZE", "DELEGATE", "SANDBOX", "READ_FS", "WEB"}),  # S5 COMPUTE (quantum_circuit_test) PULLED BACK 2026-07-01 (John). SANDBOX (run_in_sandbox) granted 2026-07-04 (SANDBOX-EXECUTION-PATH): isolated-by-construction (k3d deny-all ns), so safe to grant where EXECUTE stays denied.
    # READ_FS granted 2026-09-09 (Rule-1 Option A, signed John) -- same reasoning as
    # igor; leaf keeps parity (Option B was explicitly NOT taken).
    "leaf": frozenset({"READ", "ANALYZE", "DELEGATE", "SANDBOX", "READ_FS"}),  # Option B (John 2026-06-27): leaf MAY delegate. SANDBOX granted 2026-07-04 so a leaf can test generated code in isolation (the Brief-4 use case).
}
_DEFAULT_ROLE_CLASSES = frozenset({"READ", "ANALYZE"})


def _role_classes(role: str) -> frozenset:
    return ROLE_ENABLED_CLASSES.get(role, _DEFAULT_ROLE_CLASSES)


def allowed_tools(role: str = "igor", policy: dict | None = None) -> set:
    """Tools whose policy class is ENABLED *and* permitted for this role (B2).
    igor -> READ+ANALYZE+DELEGATE+SANDBOX+READ_FS; leaf -> the same set -- Option B
    (John 2026-06-27): the leaf role MAY delegate (recursion depth-bounded downstream). An
    UNKNOWN role fail-closes to READ+ANALYZE (no delegate_subtask, no run_in_sandbox, and
    no READ_FS host-FS readers) via _DEFAULT_ROLE_CLASSES. Counts are not written here:
    every copy went stale on the next grant (a READ tool added 2026-09-11 changed all three
    again). Compute len(allowed_tools(role)); tests/test_tool_gate_docs_drift.py fails if a
    count claim comes back.
    Intersecting with policy-enabled classes keeps disabled classes (WRITE/EXECUTE)
    off-limits for every role. role=igor reproduces the pre-B2 result exactly."""
    p = policy if policy is not None else load_policy()
    enabled = _enabled_classes(p) & _role_classes(role)
    return {t for t, c in p.get("tools", {}).items() if c in enabled}


def denied_tools(role: str = "igor", policy: dict | None = None) -> set:
    """Tools whose policy class is DISABLED (WRITE + EXECUTE) -- OFF LIMITS."""
    p = policy if policy is not None else load_policy()
    disabled = _disabled_classes(p)
    return {t for t, c in p.get("tools", {}).items() if c in disabled}


def tool_class(tool: str, policy: dict | None = None) -> str:
    """Policy class of a tool name, or 'UNKNOWN' if the policy does not class it
    (e.g. the Stack-B tools). Used for the violation record's `class` field."""
    p = policy if policy is not None else load_policy()
    return p.get("tools", {}).get(tool, "UNKNOWN")


def instruction_block(role: str = "igor", policy: dict | None = None) -> str:
    """Generated DO-NOT-USE text. Names the tools OFF-LIMITS for this role: the
    policy-denied tools (WRITE/EXECUTE) PLUS any enabled-class tools the role may not
    use (B2: a leaf's delegate_subtask). Never hand-authored -> cannot drift from policy.
    role=igor reproduces the pre-B2 block (denied tools only)."""
    p = policy if policy is not None else load_policy()
    denied = set(denied_tools(role, p))
    role_excluded = allowed_tools("igor", p) - allowed_tools(role, p)  # enabled tools this role can't use
    offlimits = sorted(denied | role_excluded)
    # B2 Option B: if the role MAY delegate, tell the model the recursion is bounded
    # (the depth/width fence is enforced in run_tool_loop; this text is advisory).
    delegate_note = ""
    if "delegate_subtask" in allowed_tools(role, p):
        delegate_note = (" Delegation via delegate_subtask is depth- and width-bounded: calls "
                         "beyond the configured recursion depth or per-turn width are refused and logged.")
    if not offlimits:
        return "All advertised tools are permitted. No tools are off limits." + delegate_note
    return (
        "These tools are OFF LIMITS. Do not call: " + ", ".join(offlimits) + ". "
        "Any attempt is logged and refused." + delegate_note
    )


def assert_no_drift(gate_allowed: Iterable[str], role: str = "igor", policy: dict | None = None) -> dict:
    """Structural confirmation of the policy-derived gate (Ceremony 3).

    HARD (raise PolicyDriftError): gate & denied_tools(role) == set()
        -- no OFF-LIMITS tool is reachable from the gate. The anti-split property
        that keeps the DO-NOT-USE block honest.
    HARD (raise PolicyDriftError): (gate - allowed_tools(role)) == set()
        -- every gate tool is policy-allowed (enabled class). Since the gate is
        derived from allowed_tools, an UNCLASSIFIED tool defaults OUT (fail-closed);
        a gate tool that is not policy-allowed is therefore a real drift -> raise.
    SOFT (no raise): returns {'unclassified': set()} -- EMPTY by construction now
        that STACKB_KNOWN is retired. A non-empty set here means C2 classification
        is incomplete or the gate is not policy-derived (the C2+C3 acceptance gate).

    Returns dict {'unclassified': set, 'allowed': set, 'denied': set}.
    """
    gate = set(gate_allowed)
    p = policy if policy is not None else load_policy()
    allowed = allowed_tools(role, p)
    denied = denied_tools(role, p)

    leak = gate & denied
    if leak:
        raise PolicyDriftError(
            "DENIED tools reachable from gate (gate & denied != empty): " + ", ".join(sorted(leak))
        )
    rogue = gate - allowed
    if rogue:
        raise PolicyDriftError(
            "rogue gate entries (not policy-allowed / unclassified): " + ", ".join(sorted(rogue))
        )
    # unclassified is empty by construction (gate <= allowed). Kept for shape compat.
    return {"unclassified": gate - allowed, "allowed": allowed, "denied": denied}


def _self_check() -> None:
    """Unit-self-check: generate for a role and assert the three consumers agree
    against the live policy. Run under __main__ or from a test; NOT at import."""
    pol = load_policy()
    allowed = allowed_tools("leaf", pol)
    denied = denied_tools("leaf", pol)
    # Consumer 1 (allowed) and Consumer 2 (denied) must be disjoint and both drawn
    # from the policy tool map.
    assert allowed.isdisjoint(denied), "allowed and denied overlap"
    assert (allowed | denied) <= set(pol["tools"]), "allowed|denied not subset of policy tools"
    # Consumer 3 (instruction block) must name exactly the denied tools. Parse the LIST itself:
    # the block goes on after it (4cc383c added the delegate note, which names delegate_subtask,
    # an ALLOWED tool), so a substring test on everything after "Do not call:" misfired and this
    # self-check failed from 4cc383c on (found 2026-09-11; nothing ran it).
    block = instruction_block("leaf", pol)
    listed = set()
    if "Do not call:" in block:
        _list_text = block.split("Do not call:", 1)[1].split(". Any attempt", 1)[0]
        listed = {n.strip() for n in _list_text.split(",") if n.strip()}
    for t in denied:
        assert t in listed, f"denied tool {t} missing from instruction block"
    for t in allowed:
        assert t not in listed, f"allowed tool {t} wrongly in DO-NOT-USE list"
    # Drift guard (C3): the gate is now POLICY-DERIVED (== allowed enabled-class
    # tools), so it must pass both HARD tiers and the SOFT warn-set is EMPTY by
    # construction (the C2+C3 acceptance signal).
    gate = allowed_tools("leaf", pol)
    res = assert_no_drift(gate, "leaf", pol)
    assert res["unclassified"] == set(), res["unclassified"]
    print("policy_instructions self-check PASS")
    print("  allowed:", sorted(allowed))
    print("  denied :", sorted(denied))
    print("  block  :", block)
    print("  drift  : HARD-tiers pass; unclassified(warn):", sorted(res["unclassified"]))


if __name__ == "__main__":
    _self_check()
