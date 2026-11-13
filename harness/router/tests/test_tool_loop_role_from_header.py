"""STEP 0 (2026-09-26): the X-Tool-Loop header value selects the loop's POLICY ROLE.

These are the checks that make step 0 a fence rather than a claim. Before step 0, main.py called
run_tool_loop with no role= argument, so every enrolled caller got the default role "igor" -- the
full READ+ANALYZE+DELEGATE+SANDBOX+READ_FS set. The per-caller machinery existed and was never
reached.

The load-bearing test here is test_proposer_role_cannot_delegate, and it is deliberately TWO-SIDED:
it asserts delegate_subtask is ABSENT for "proposer" *and* PRESENT for "igor". A one-sided version
would still pass if the policy load failed and every role resolved to the empty set -- a fence that
passes because nothing is allowed at all is not a fence, it is a blind check.
"""
import pytest

import igor_tool_loop as itl
import main as m
import policy_instructions as pi


class _H:
    """Minimal stand-in for Starlette's case-insensitive header mapping.

    Only .get(name, default) is used by _tool_loop_role_from_header. Real requests arrive with
    Starlette's own mapping, which lowercases names; the production call passes request.headers
    directly, so this stub only has to honor the same .get contract.
    """

    def __init__(self, value=None):
        self._v = value

    def get(self, name, default=None):
        if name == "X-Tool-Loop" and self._v is not None:
            return self._v
        return default


# --------------------------------------------------------------------------- the header map
@pytest.mark.parametrize("value,expected", [
    ("true", "igor"),          # CEREMONY-92's only accepted value -- must stay byte-compatible
    ("TRUE", "igor"),          # the old parse lowercased, so this still enrolls
    ("  true  ", "igor"),      # the old parse stripped
    ("proposer", "proposer"),  # step 0's new, strictly smaller role
    ("PROPOSER", "proposer"),
])
def test_accepted_header_values_map_to_a_role(value, expected):
    assert m._tool_loop_role_from_header(_H(value)) == expected


@pytest.mark.parametrize("value", [
    None,          # header absent
    "",
    "   ",
    "false", "0", "no",
    "leaf",        # EXCLUDED on purpose: same class set as igor, but a caller may not claim it
    "igor",        # the role name itself is not an accepted header value; "true" is
    "admin", "root", "../igor", "proposer,leaf",
])
def test_everything_else_does_not_enroll(value):
    """None is the not-enrolled answer, so enrollment and the role are the SAME lookup and cannot
    disagree. Anything unrecognized behaves exactly as it did before step 0: no loop."""
    assert m._tool_loop_role_from_header(_H(value)) is None


def test_header_map_is_closed_and_offers_no_role_above_igor():
    """A caller must not be able to name a role of its own choosing, and no reachable role may hold
    a class that "true" (igor) does not already grant."""
    igor_classes = pi.ROLE_ENABLED_CLASSES["igor"]
    for value, role in m._TOOL_LOOP_HEADER_ROLES.items():
        granted = pi._role_classes(role)
        assert granted <= igor_classes, (
            "header value %r selects role %r whose classes %s exceed igor's %s"
            % (value, role, sorted(granted - igor_classes), sorted(igor_classes)))


# --------------------------------------------------------------------------- the fence itself
def test_proposer_role_cannot_delegate():
    """THE fence. Two-sided on purpose -- see the module docstring."""
    igor = itl._safe_for_role("igor")
    proposer = itl._safe_for_role("proposer")
    assert "delegate_subtask" in igor, (
        "control failed: igor should be able to delegate, so a proposer lacking it proves nothing")
    assert "delegate_subtask" not in proposer
    assert proposer, "a proposer with NO tools at all would make this test vacuous"
    assert proposer < igor, "the proposer set must be strictly smaller than igor's"


def test_proposer_role_is_deliberately_unnamed_so_it_fail_closes():
    """The fence relies on the UNKNOWN-role default, not on a hand-authored allowlist -- the same
    choice made for bifrost-npu-tools. If someone later adds "proposer" to ROLE_ENABLED_CLASSES,
    this fails loudly rather than silently widening the pilot's surface."""
    assert "proposer" not in pi.ROLE_ENABLED_CLASSES
    assert pi._role_classes("proposer") == frozenset({"READ", "ANALYZE"})


def test_proposer_role_has_no_sandbox_and_no_host_fs_readers():
    proposer = itl._safe_for_role("proposer")
    for denied in ("run_in_sandbox", "read_file", "grep_repo", "list_dir", "git_read"):
        assert denied not in proposer, denied


# --------------------------------------------------------------------------- the mode crash fix
@pytest.mark.parametrize("mode,expected", [
    ("tools", "tools"),
    ("TOOLS", "tools"),
    ("reasoning", "reasoning"),
    ("banana", "banana"),          # an unknown VALUE is routed as reasoning downstream, not here
    (None, "reasoning"),
    ("", "reasoning"),
    (123, "reasoning"),            # non-string: the old `.lower()` raised AttributeError
    (["tools"], "reasoning"),
    ({"mode": "tools"}, "reasoning"),
    (True, "reasoning"),
])
def test_mode_of_never_raises_on_a_non_string(mode, expected):
    assert itl._mode_of({"mode": mode}) == expected


def test_mode_of_handles_a_missing_key():
    assert itl._mode_of({}) == "reasoning"


def test_the_old_mode_expression_really_did_raise():
    """Pins WHY _mode_of exists. If this ever stops raising, the guard is no longer load-bearing
    and the reason for it should be re-examined rather than carried as folklore."""
    with pytest.raises(AttributeError):
        (({"mode": ["tools"]}.get("mode") or "reasoning").lower())


# --------------------------------------------------------------------------- the WIRING
def test_main_actually_passes_the_role_to_run_tool_loop():
    """The fence is only real if the computed role REACHES the loop.

    Everything above would still pass if main.py computed _tool_loop_role and then never used it --
    the role would be dropped and run_tool_loop would fall back to its own default, "igor". This is
    a source-level check in the same spirit as test_tool_gate_docs_drift: it cannot prove the value
    is correct at run time, but it does fail if the argument is deleted, which is the realistic
    regression.
    """
    import inspect
    import re
    src = inspect.getsource(m)
    call = re.search(r"await run_tool_loop\((.*?)\n\s*\)", src, re.S)
    assert call, "could not find the run_tool_loop call in main.py"
    body = call.group(1)
    assert "role=" in body, (
        "main.py calls run_tool_loop without a role= argument, so every enrolled caller "
        "silently gets the default role 'igor' -- this is exactly the step-0 defect")
    assert "_tool_loop_role" in body, (
        "run_tool_loop receives a role= but not the one derived from the header")


def test_both_header_read_sites_use_the_same_helper():
    """Two read sites 688 lines apart previously each re-parsed the header. If one is changed and
    the other is not, enrollment and the role disagree -- the drift this helper exists to prevent."""
    import inspect
    src = inspect.getsource(m)
    assert src.count("_tool_loop_role_from_header(request.headers)") == 2, (
        "expected exactly 2 call sites of the header helper")
    assert 'request.headers.get("X-Tool-Loop"' not in src, (
        "a raw X-Tool-Loop header parse is back in main.py; it must go through the helper")
