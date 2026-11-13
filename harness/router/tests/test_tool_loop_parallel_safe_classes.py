"""F13 tripwire (2026-09-11): phase 2 of run_tool_loop gathers EVERY dispatch of a turn in
parallel (asyncio.gather). That is safe only while no tool a role can reach has a side effect
that another call in the same turn could observe or race.

Today every class any role can reach is read-only or isolated: READ, READ_FS, ANALYZE,
DELEGATE (independent leaf loops), SANDBOX (a fresh pod per call) and WEB (an outbound
GET that returns a value and writes nothing). WRITE, EXECUTE and
COMPUTE are not enabled for any role. If a policy change ever grants one of them, this test
fails on purpose: add the per-tool parallel-safe flag and the sequential lane described in
DESIGN-igor-parallel-safe-dispatch-F13-2026-09-11 BEFORE enabling it.

Adopted 2026-09-25 on John's "Q42 - yes proceed", with the per-host qualification that answer
covered: see test_delegate_is_parallel_safe_only_because_forge_is_capped. The design's option B
was staged 09-11 and deliberately held, because asserting DELEGATE parallel-safe UNCONDITIONALLY
would have frozen a 102 GB Forge hazard into a passing test. The cap ceremony that closes that
hazard is live (v455, igor_tool_loop.py commit fcd0d01), so B is now true -- and the last test
here binds the claim to the mechanism that makes it true rather than asserting it on faith.
"""
import igor_tool_loop as itl

# WEB added 2026-10-08 (Ceremony 107, signed John, option b). The membership test for
# this frozenset is NOT "is the class enabled" but "can two calls in one turn observe
# or race each other", because phase 2 gathers every dispatch concurrently. WEB has one
# member, web_search, which is an outbound HTTP GET: it returns a value and writes
# nothing, so two concurrent calls cannot interact. As with DELEGATE below, the claim is
# bound to its mechanism by a test rather than left as an assertion of faith.
PARALLEL_SAFE_CLASSES = frozenset({"READ", "READ_FS", "ANALYZE", "DELEGATE", "SANDBOX",
                                   "WEB"})
ROLES = ("igor", "leaf", "no-such-role")


def _unsafe(role, class_map):
    return sorted(t for t in itl._safe_for_role(role)
                  if class_map.get(t, "UNKNOWN") not in PARALLEL_SAFE_CLASSES)


def test_every_gated_tool_is_parallel_safe_today():
    assert itl._TOOL_CLASS_MAP, "policy did not load; the tripwire would be blind"
    for role in ROLES:
        assert _unsafe(role, itl._TOOL_CLASS_MAP) == [], (
            f"role {role!r} can reach a tool outside {sorted(PARALLEL_SAFE_CLASSES)}; "
            "phase 2 would run it in parallel with the rest of the turn (see F13 design)")


def test_the_tripwire_fires_on_a_write_class():
    gated = sorted(itl._safe_for_role("igor"))
    assert gated
    planted = dict(itl._TOOL_CLASS_MAP)
    planted[gated[0]] = "WRITE"
    assert _unsafe("igor", planted) == [gated[0]]


def test_an_unclassified_tool_counts_as_unsafe():
    gated = sorted(itl._safe_for_role("igor"))
    planted = {k: v for k, v in itl._TOOL_CLASS_MAP.items() if k != gated[0]}
    assert _unsafe("igor", planted) == [gated[0]]


def test_delegate_is_parallel_safe_only_because_forge_is_capped():
    """DELEGATE sits in PARALLEL_SAFE_CLASSES because leaf loops do not OBSERVE each other.

    They do, however, share HARDWARE, and that is a different question from side-effect
    isolation. Four of the SAFE_LEAVES are Forge-hosted and phase 2 gathers dispatches
    concurrently, so with no host cap one turn could put Igor (65 GB) + gemma4 (17 GB) +
    toolmaster (20 GB) = 102 GB on a 100 GB GTT -- the combination bifrost_delegate.py forbids
    in capitals, and against the 2026-09-17 ruling that Igor and T2 share one RAM bus and run
    sequentially.

    So DELEGATE's membership above is TRUE ONLY WHILE THE CAP EXISTS. Assert the mechanism, not
    just the conclusion: removing the cap must fail this tripwire rather than silently
    re-opening the hazard the tripwire is supposed to guard.
    """
    assert itl._FORGE_LEAVES, (
        "_FORGE_LEAVES is empty, so the Forge host cap has nothing to key on and DELEGATE's "
        "presence in PARALLEL_SAFE_CLASSES is unsupported")
    assert itl._FORGE_CONCURRENCY_CAP == 1, (
        f"_FORGE_CONCURRENCY_CAP is {itl._FORGE_CONCURRENCY_CAP}, not 1: more than one "
        "Forge-hosted leaf could run concurrently on one RAM bus")
    assert itl.DELEGATE_WIDTH_CAP <= 2, (
        f"DELEGATE_WIDTH_CAP is {itl.DELEGATE_WIDTH_CAP}; the gather makes this a concurrency "
        "cap, and it was reduced to 2 precisely to bound total fan-out")


def test_web_is_parallel_safe_only_while_it_is_a_single_read_only_tool():
    """WEB sits in PARALLEL_SAFE_CLASSES because its only member is a read-only GET.

    That is a statement about the CLASS's contents, not about the class name, so it stops
    being true the moment WEB acquires a second tool -- a fetch-and-cache, a POST, anything
    that writes. Assert the mechanism: if WEB ever classifies more than web_search, this
    fails and the per-tool parallel-safe flag from the F13 design has to come first.
    """
    web_tools = sorted(t for t, c in itl._TOOL_CLASS_MAP.items() if c == "WEB")
    assert web_tools == ["web_search"], (
        f"WEB now classifies {web_tools}; its presence in PARALLEL_SAFE_CLASSES rests on "
        "web_search being a single read-only GET. Re-derive the claim before widening it.")
