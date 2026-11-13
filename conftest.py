"""Make the repository importable to its own test suite.

WHY THIS FILE EXISTS, measured rather than asserted. CI runs `python -m pytest -q --tb=line`
with no path argument, i.e. the whole repository. Run that way without this file, collection
fails with 104 errors. Of the 25 distinct module names in those errors, 22 are modules that
ARE in this repository -- the failure is not missing code, it is that nothing puts the two
directories holding it on `sys.path`.

The suite imports in two shapes and needs both:

  `import telemetry`, `import config`, `import backends.ollama`
      Router modules and the `backends` package, by BARE name. They live in harness/router/,
      which nothing adds, because pytest inserts the directory containing each test file
      (tests/ has no __init__.py, so tests/ is what gets inserted) -- never the router's.

  `from shared.resolver import resolve`
      The endpoint registry, as a package path from the repository root. A conftest.py's own
      directory is what pytest inserts for it, so placing this file AT THE ROOT is what makes
      that import work; `shared/` has no __init__.py and resolves as a namespace package.

ORDER IS DELIBERATE: the root goes on first so that `shared.resolver` always resolves from the
repository, and harness/router goes in front of it, because the bare-name imports are the ones
that cannot fall back to anything else.

CHECKED BEFORE WRITING, because prepending a directory full of modules to sys.path can shadow
the standard library and break imports that have nothing to do with these tests: neither this
directory nor harness/router contains a module or package whose name is in
`sys.stdlib_module_names`. If a future module here is named `types.py`, `json.py` or the like,
that stops being true and this file becomes the cause of a confusing failure elsewhere -- which
is why the test beside it asserts the property rather than leaving it to this comment.

This is a TEST-TIME path fix and nothing else. It is not an installation story: a packaging
layout that made these importable without it would be better, and this file does not pretend
to be that. It is the smallest artifact that lets the suite collect.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Front of the path, router first. Inserting at 0 in this order leaves harness/router ahead of
# the root, which is what the bare-name imports need.
for _path in (ROOT, os.path.join(ROOT, "harness", "router")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ---------------------------------------------------------------------------
# fastmcp is NOT pinned in requirements.txt, deliberately: it cannot co-install with the core
# pins (measured ResolutionImpossible -- requirements-mcp.txt carries the evidence). Three
# modules import it at module scope (bifrost_shell, bifrost_shell_static, bifrost_delegate),
# all three belong to the shell-MCP service rather than the router, and two test files reach
# them. An ImportError during COLLECTION aborts the entire run, so on a default checkout those
# two files stop 1968 unrelated tests from ever executing.
#
# THE EXCLUSION IS CONDITIONAL, NOT BLANKET. Where fastmcp IS importable -- the shell-MCP
# service's own environment -- both files collect and run exactly as before. A hard-coded
# exclusion would instead hide a real regression from the only environment equipped to catch
# it, which is the opposite of what excluding a test is for. find_spec is used rather than an
# import so that deciding this costs nothing and has no side effects.
import importlib.util

# The list is EMPIRICAL, taken from an observed run on 2026-11-13, not from a name search.
# A grep for these module names matches 13 test files, but 7 of them only mention the names
# (in strings, or by reading the source rather than importing it) and pass perfectly well
# without fastmcp -- excluding those would throw away working coverage to tidy a count. The
# six below are the ones that produce NOTHING without it: two fail at import during collection,
# and the other four were measured at 105 errors and ZERO passes when run on their own.
#
# WHAT CATCHES A STALE LIST -- AND WHAT DOES NOT. An earlier version of this comment claimed
# that a seventh file joining the family would push the suite's error count off zero and say
# so. That is only true of a file that imports fastmcp at MODULE scope. A file importing it
# inside a test body collects normally and produces a FAILURE, so the error count stays at
# zero and nothing is said. `test_bundle_a_security_policy::test_shell_read_fs_gating` is
# already that case -- a seventh member of this family, failing on ModuleNotFoundError, which
# the claim above would have had you believe was impossible.
#
# So: a collection-time arrival is caught, by the error count leaving zero. A runtime arrival
# is NOT caught here, and is left to the one place that can see it -- reading why a test
# failed. No count is pinned either way: a pinned expectation gets edited to match whatever
# the run produced, which is how an assertion stops being one.
_NEEDS_FASTMCP = (
    "test_bifrost_shell_gates.py",
    "test_breach_guard_followups_router.py",
    "test_delegate_safe_leaves.py",
    "test_shell_delegate_structured_error.py",
    "test_shell_delegate_subtask_allowlist.py",
    "test_tool_loop_delegate_mode_arg.py",
)

if importlib.util.find_spec("fastmcp") is None:
    collect_ignore = [
        os.path.join(ROOT, "harness", "router", "tests", _n) for _n in _NEEDS_FASTMCP
    ]
