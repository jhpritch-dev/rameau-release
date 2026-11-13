"""D9 COMPARISON: the per-machine semaphore must come from identity, not from an address.

WHY THIS TEST CAN EXIST AT ALL -- it is the argument for the design, not a side effect of it.
The code this replaces matched tiers[].base_url against three IP literals. A regression test
for THAT could only be written by putting the fleet's real addresses in this file, in a
repository whose whole purpose is publication. So the old design was untestable here, which
is why the genericize broke it silently and why nothing caught it: P5 scored 47/47 with the
branch already unable to match anything.

Keying off `machine` makes every assertion a statement about machine NAMES. No address
appears below, and test_no_address_literal_selects_a_semaphore fails if one is introduced.

What is asserted:
  1. _build_tier_backends surfaces `machine` for every tier.
  2. Every DERIVED tier on one local machine holds the same semaphore OBJECT -- identity, not
     slot count. This is the assertion the defect defeats: _SEM_HEARTH is Semaphore(2) and the
     old else-arm built Semaphore(2), so comparing counts shows nothing while four tiers
     quietly stop sharing one pool and the dGPU ceiling goes 2 -> 8.
  3. Cloud tiers get a pool EACH -- the old else-arm's behavior, preserved deliberately:
     nine providers do not contend with one another.
  4. An unidentifiable machine is FATAL, not defaulted. Both recorded failures of this code
     ended in a catch-all `else` that meant "cloud" AND "could not tell".
  5. No address literal reaches the semaphore selection arm.
"""
import ast
import re
import threading

import pytest

pytest.importorskip("langgraph", reason="autopilot_graph needs langgraph; runs in the image")

import autopilot_graph as ag  # noqa: E402
from config import get_tier_backends  # noqa: E402

LOCAL_MACHINES = ("forge", "hearth", "bifrost")


def _hand_tuned_keys():
    """The literal _TIER_SEMAPHORES keys, read from source by AST.

    NOT read from the live dict: _derive_tier_registry() runs at autopilot_graph import and
    has already folded every fleet_config tier into it by the time this module loads, so
    set(ag._TIER_SEMAPHORES) would be ALL tiers and would make the pool-identity test below
    pass vacuously.

    The declaration is an AnnAssign (`_TIER_SEMAPHORES: dict[...] = {...}`), not an Assign.
    Walking for Assign alone returns nothing and reads as "0 pre-seeded" -- a wrong answer
    that looks like a measurement.
    """
    tree = ast.parse(open(ag.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        tgt = getattr(node, "target", None)
        if isinstance(node, ast.AnnAssign) and getattr(tgt, "id", "") == "_TIER_SEMAPHORES":
            return {k.value for k in node.value.keys}
    raise AssertionError("could not find the _TIER_SEMAPHORES literal in %s" % ag.__file__)


HAND_TUNED = _hand_tuned_keys()


def _backends():
    return {t.value if hasattr(t, "value") else str(t): b
            for t, b in get_tier_backends().items()}


def test_the_hand_tuned_set_is_a_real_subset():
    """Self-check on the probe above, so the pool test cannot pass by measuring nothing."""
    assert HAND_TUNED, "no hand-tuned keys found; every test below would be vacuous"
    all_tiers = set(_backends())
    assert HAND_TUNED < all_tiers or HAND_TUNED <= all_tiers, \
        "hand-tuned keys are not a subset of the configured tiers: %s" % sorted(
            HAND_TUNED - all_tiers)
    assert len(HAND_TUNED) < len(all_tiers), (
        "every configured tier is hand-tuned (%d of %d), so nothing is derived and the "
        "pool-identity assertions test nothing" % (len(HAND_TUNED), len(all_tiers)))


def test_every_tier_carries_machine():
    missing = sorted(t for t, b in _backends().items() if not (b or {}).get("machine"))
    assert not missing, (
        "tiers with no `machine` in the backend dict: %s -- _build_tier_backends must "
        "surface it, or _derive_tier_registry refuses to start" % missing)


@pytest.mark.parametrize("machine", LOCAL_MACHINES)
def test_one_local_machine_means_one_pool(machine):
    """Pool IDENTITY, not slot count. Tiers that must contend must hold one object.

    Hand-tuned tiers are excluded because the literal map deliberately splits some machines:
    Bifrost small/large by model VRAM, and Hearth's Vega 8 iGPU onto its own pool. Those are
    tunings no config field can express, which is why they stay hand-held.
    """
    derived = {t: id(ag._TIER_SEMAPHORES[t])
               for t, b in _backends().items()
               if (b or {}).get("machine") == machine
               and t not in HAND_TUNED and t in ag._TIER_SEMAPHORES}
    if not derived:
        pytest.skip("no derived %s tiers in this fleet_config" % machine)
    assert len(set(derived.values())) == 1, (
        "%s derived tiers hold %d distinct semaphore objects: %r. Every derived tier on one "
        "machine must share one pool; distinct objects mean the machine's concurrency cap is "
        "bypassable, which is the D9 COMPARISON defect." % (
            machine, len(set(derived.values())), derived))
    assert next(iter(derived.values())) == id(ag._SEM_OF_MACHINE[machine]), (
        "%s derived tiers do not hold _SEM_OF_MACHINE[%r]" % (machine, machine))


def test_cloud_tiers_get_a_pool_each():
    derived = [t for t, b in _backends().items()
               if (b or {}).get("machine") == "cloud"
               and t not in HAND_TUNED and t in ag._TIER_SEMAPHORES]
    ids = [id(ag._TIER_SEMAPHORES[t]) for t in derived]
    assert len(set(ids)) == len(ids), (
        "cloud tiers share semaphore objects: %r. Each cloud tier takes its own "
        "Semaphore(2); sharing one pool across providers is a behavior change." % derived)


def _select(backend):
    """Reproduce the selection arm of _derive_tier_registry for one backend dict."""
    machine = str((backend or {}).get("machine") or "")
    if machine in ag._SEM_OF_MACHINE:
        return ag._SEM_OF_MACHINE[machine]
    if machine == "cloud":
        return threading.Semaphore(2)
    raise RuntimeError("tier has machine=%r, not a known fleet machine" % (machine or None,))


@pytest.mark.parametrize("bad", [{"machine": "not-a-fleet-machine"}, {"machine": ""}, {}, None])
def test_unknown_machine_is_fatal_not_defaulted(bad):
    """The class of this defect is a catch-all `else`. There must not be one."""
    with pytest.raises(RuntimeError) as exc:
        _select(bad)
    assert "machine" in str(exc.value).lower()


def test_no_address_literal_selects_a_semaphore():
    """Guards the design, not just today's code.

    If a future edit reintroduces address matching, this fails even on a machine where the
    addresses happen to match -- which is precisely the failure mode that produced D9: the
    literals and the config were rewritten together, so they still agreed and nothing broke
    until one side changed.
    """
    src = open(ag.__file__, encoding="utf-8").read()
    start = src.index("_machine = str(")
    arm = src[start:src.index("_TIER_SEMAPHORES[_tier] = _sem", start)]
    assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", arm), \
        "an address literal appeared in the semaphore selection arm"
    assert not re.search(r"\d{1,3}\.\d{1,3}\.[xX*]\.", arm), \
        "a genericized address placeholder appeared in the semaphore selection arm"
    assert "base_url" not in arm, \
        "the selection arm reads base_url again; it must key off `machine`"
