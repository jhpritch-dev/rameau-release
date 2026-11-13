"""The policy tool map has ONE more entry than bifrost_shell.py has gated functions.

bifrost_shell_policy.json classes 20 tools; bifrost_shell.py carries 19 @gated_tool
functions. The extra entry is tool_output_page (class READ), and the gap is BY DESIGN:
the tool is served in-process by igor_tool_loop.run_tool_loop, from that loop's own
_page_store, and the loop `continue`s at PAGE_TOOL_NAME BEFORE the gateway dispatch.
A @gated_tool copy would therefore be unreachable from the loop and could not see the
store anyway -- it would only widen the MCP surface for nothing.

The policy entry is LOAD-BEARING, not vestigial: policy_instructions.allowed_tools()
derives names purely from policy["tools"], so deleting it empties the paging gate and
silently reverts long tool results to a plain cut.

CONSEQUENCE WORTH KNOWING, and asserted below: because this tool never passes through
gated_tool, neither the bifrost_halt kill switch nor the bifrost-gates.toml overlay
reaches it. Pulling the switch stops 19 tools, not 20. That gap is real and is tracked
separately; this file pins the SHAPE so an unexplained 21st entry, or a silent change
to which tools are gated, fails loudly here.
"""
import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A real decorator: @gated_tool("CLASS") alone on a line. The mentions inside the
# comment block at the top of bifrost_shell.py are deliberately not matched -- three
# guards written on 2026-09-21 tripped on their own explanatory prose.
_DECORATOR = re.compile(r'(?m)^@gated_tool\(\s*"([A-Z_]+)"\s*\)\s*$')
_DECORATED = re.compile(
    r'(?m)^@gated_tool\(\s*"[A-Z_]+"\s*\)\s*\n(?:@[\w.]+(?:\([^)]*\))?\s*\n)*'
    r'(?:async\s+)?def\s+(\w+)\s*\(')

PAGE_TOOL = "tool_output_page"
# Ceremony 107 (2026-10-08): web_search is the SECOND tool served in-process by
# run_tool_loop rather than by a @gated_tool function in bifrost_shell.py. It is in the
# policy because allowed_tools() derives every name from policy["tools"], and it has no
# gated function because its executor reuses search_query.py and settings.searxng_url,
# neither of which the gateway container can reach. The consequence this file documents
# applies to it too, which is why test_the_kill_switch_reaches_web_search exists below.
WEB_TOOL = "web_search"
IN_PROCESS_TOOLS = {PAGE_TOOL, WEB_TOOL}


def _shell_src():
    with open(os.path.join(_ROOT, "bifrost_shell.py"), encoding="utf-8",
              errors="replace") as fh:
        return fh.read()


def _policy():
    import json
    with open(os.path.join(_ROOT, "bifrost_shell_policy.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_decorator_scan_actually_finds_something():
    """Guard the guard. If the regex stops matching, every assertion below passes
    vacuously -- which is exactly how a guard ends up believed and dead."""
    src = _shell_src()
    assert len(_DECORATOR.findall(src)) >= 15, \
        "found %d decorators; the scan is broken" % len(_DECORATOR.findall(src))
    assert len(_DECORATED.findall(src)) == len(_DECORATOR.findall(src)), \
        "a @gated_tool decorator is not directly above a def"


def test_policy_map_is_the_gated_functions_plus_exactly_the_page_tool():
    gated = set(_DECORATED.findall(_shell_src()))
    policy_tools = set(_policy().get("tools", {}))

    extra = policy_tools - gated
    assert extra == IN_PROCESS_TOOLS, (
        "policy entries with no @gated_tool function in bifrost_shell.py: %r. Only %s "
        "may appear here (served in-process by igor_tool_loop). Anything else is a "
        "grant with no implementation." % (sorted(extra), sorted(IN_PROCESS_TOOLS)))

    missing = gated - policy_tools
    assert missing == set(), (
        "@gated_tool functions with no policy class -- they would fail closed out of "
        "every role's gate: %r" % sorted(missing))


def test_the_page_tool_has_no_gated_function_and_must_not_gain_one():
    assert PAGE_TOOL not in _shell_src(), (
        "%s appeared in bifrost_shell.py. The loop serves it in-process and `continue`s "
        "before the gateway dispatch, so a gateway-side copy is unreachable from "
        "run_tool_loop and cannot see the per-loop _page_store. It would only widen the "
        "MCP surface." % PAGE_TOOL)


def test_the_policy_entry_is_the_grant_for_paging():
    """Removing the entry does not just lose a name -- it turns paging off."""
    pi = pytest.importorskip("policy_instructions")
    pol = _policy()
    assert pol["tools"][PAGE_TOOL] == "READ"

    stripped = {"classes": pol["classes"],
                "tools": {k: v for k, v in pol["tools"].items() if k != PAGE_TOOL}}
    for role in ("igor", "leaf", "no-such-role"):
        assert PAGE_TOOL in pi.allowed_tools(role, pol), \
            "%s is not granted to role %r -- paging would never arm" % (PAGE_TOOL, role)
        assert PAGE_TOOL not in pi.allowed_tools(role, stripped), \
            "removing the policy entry did NOT revoke the grant; the derivation has " \
            "another source and this test no longer proves what it claims"


def test_the_kill_switch_reaches_the_page_tool():
    """INVERTED by Ceremony 6 (2026-09-21), as the previous version instructed.

    This test used to assert the gap EXISTED: igor_tool_loop served tool_output_page
    without consulting bifrost_halt, because the branch `continue`s before the gateway
    dispatch and so never reaches gated_tool. Ceremony 6 added the check, so the
    assertion flips rather than disappearing -- a closed gap that stops being tested is
    a gap waiting to reopen.

    It deliberately checks PLACEMENT, not just presence. Grepping for the module name
    would pass on a comment, or on an import with no call site, which is the shape of a
    guard that is believed and dead.
    """
    loop = os.path.join(_ROOT, "igor_tool_loop.py")
    if not os.path.exists(loop):
        pytest.skip("router igor_tool_loop.py not in this checkout")
    src = open(loop, encoding="utf-8", errors="replace").read()

    assert "import bifrost_halt as _bifrost_halt" in src, (
        "igor_tool_loop.py no longer imports bifrost_halt. Ceremony 6 added it so a "
        "halt would stop tool_output_page too; without it a halt stops 19 tools, not 20.")

    i_page = src.find("if name == PAGE_TOOL_NAME:")
    assert i_page != -1, "the page dispatch branch moved; this test can no longer locate it"
    i_serve = src.find("_page_reads += 1", i_page)
    assert i_serve != -1, "the page dispatch no longer increments _page_reads"

    between = src[i_page:i_serve]
    assert "_bifrost_halt" in between and "halted()" in between, (
        "the halt is not consulted BETWEEN matching the page tool and serving it. The "
        "import alone is not the control -- the check has to run before _page_read, or "
        "a halt still does not reach this tool.")


def test_the_page_halt_check_is_documented_as_inert_until_the_router_is_rebuilt():
    """The source is right; production is not yet, and that difference is load-bearing.

    The live router image (built before 2026-09-21) contains no bifrost_halt.py --
    verified inside the pod. The import is guarded, so _bifrost_halt is None there and
    the check above is INERT until the router image is rebuilt. That is exactly the
    Ceremony 5 packaging trap, and the mitigation that actually worked last time was
    writing it down where the next reader trips over it.

    Delete this test when the router has been rebuilt and the module ships.
    """
    loop = os.path.join(_ROOT, "igor_tool_loop.py")
    if not os.path.exists(loop):
        pytest.skip("router igor_tool_loop.py not in this checkout")
    src = open(loop, encoding="utf-8", errors="replace").read()
    assert "DEPLOYMENT NOTE" in src and "INERT" in src, (
        "the comment recording that this check is inert until the router image carries "
        "bifrost_halt.py has been removed. Either the router was rebuilt -- in which "
        "case delete this test -- or a real caveat was quietly dropped.")


def test_the_kill_switch_reaches_web_search():
    """Ceremony 107 closure, and the reason this file is the one that caught the gap.

    The docstring above states that a tool which never passes through gated_tool is not
    reached by bifrost_halt or the bifrost-gates.toml overlay. web_search is served
    in-process, so it inherits exactly that exemption -- and unlike the page tool it makes
    outbound requests, so an operator halt that did not stop it would be the worst version
    of the gap. Ceremony 6 established the pattern for tool_output_page; this asserts it
    for web_search, by PLACEMENT rather than presence: the check has to run between
    matching the tool and planning its dispatch, or a halt still does not reach it.
    """
    loop = os.path.join(_ROOT, "igor_tool_loop.py")
    if not os.path.exists(loop):
        pytest.skip("router igor_tool_loop.py not in this checkout")
    src = open(loop, encoding="utf-8", errors="replace").read()

    i_branch = src.find('elif name == "web_search":')
    assert i_branch != -1, "the web_search branch moved; this test can no longer locate it"
    i_dispatch = src.find('"coro": _web_search()', i_branch)
    assert i_dispatch != -1, "the web_search branch no longer plans a _web_search coroutine"

    between = src[i_branch:i_dispatch]
    assert "_bifrost_halt" in between and "halted()" in between, (
        "the halt is not consulted BETWEEN matching web_search and planning its dispatch. "
        "web_search never reaches gated_tool, so without this check an operator halt stops "
        "every gateway tool and leaves the one tool that talks to the public internet running.")
    assert "continue" in between, (
        "the halted path does not `continue`; it would fall through and dispatch anyway")
