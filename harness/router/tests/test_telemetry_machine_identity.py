"""telemetry.machine: the row must name the host that wrote it.

Background. Until 2026-09-09 telemetry.py opened with two module-level literals:

    MACHINE  = "bifrost"
    HARDWARE = "bifrost-3950x-9070xt-rdna4"

Every InferenceEvent, RoutingDecision, EmbedEvent and PipelineStageEvent took them as a
pydantic field default, so every row in the DB claimed Bifrost. Measured on the live DB
before the fix -- exactly one distinct value per table:

    inference_events   43,300 / 43,300      embed_events     86,042 / 86,042
    routing_decisions  41,678 / 41,678      pipeline_stages       99 /      99

and the claim was false even about the writer: the router pod runs in the k3d cluster whose
API server is https://<lan-host>:49442, which is HEARTH. The column never once named the
host that wrote the row, which silently invalidates every per-host number derived from it.

What these tests pin, and why each one exists:

  * The resolver is HOST-SENSITIVE. The defect was not a wrong value, it was a CONSTANT --
    so the load-bearing assertion is that three different hosts produce three different
    answers. A test that only checked `resolve_machine() == "bifrost"` on the dev box would
    have passed against the broken code.
  * An unresolvable host yields the sentinel and NEVER "bifrost". Silently defaulting to a
    real machine name is the original defect; a row that cannot be attributed must say so.
  * `socket.gethostname()` is not a machine name inside a container -- in the router pod it
    returns the POD name, which changes on every redeploy. The env var is the only mechanism
    that works there, so it must win over the hostname.
  * The vocabulary is fleet_config.json's, not a new one. test_vocabulary_matches_fleet_config
    fails if anyone adds a fourth name here without it existing in the fleet's own tier map.

`machine` means THE HOST THAT WROTE THE ROW -- deliberately not the host that served the
inference. That second question is answered by tier_used joined to fleet_config tiers[].machine,
and conflating the two is how the column became useless in the first place.
"""
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telemetry  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# FAKE HOSTNAMES, and the test is better for it. These used to be the two real machine
# names, measured by running `hostname` on each box -- which published them, in a file whose
# whole subject is not leaking machine identity into telemetry.
#
# What is under test is the MECHANISM: that a hostname in the configured table maps to its
# fleet label, that an unknown one does not, and that BIFROST_MACHINE overrides both. None
# of that needs a real name, so the table is injected below and these are arbitrary.
BIFROST_HOST = "host-alpha"
HEARTH_HOST = "host-beta"        # stands for the host that runs the router pod
FORGE_HOST = "forge"
telemetry._FLEET_HOSTNAMES = {
    BIFROST_HOST: "bifrost",
    HEARTH_HOST: "hearth",
    FORGE_HOST: "forge",
}
# The router pod's real hostname, measured 2026-09-09. Names no machine.
POD_HOSTNAME = "bifrost-router-77fbbbb584-jbjtm"


class _As:
    """Run a block as if this process were on `hostname`, with BIFROST_MACHINE set to `env`."""

    def __init__(self, hostname=None, env=None):
        self.hostname, self.env = hostname, env

    def __enter__(self):
        self._gh = socket.gethostname
        self._env = os.environ.get("BIFROST_MACHINE")
        if self.hostname is not None:
            socket.gethostname = lambda: self.hostname
        os.environ.pop("BIFROST_MACHINE", None)
        if self.env is not None:
            os.environ["BIFROST_MACHINE"] = self.env
        return self

    def __exit__(self, *exc):
        socket.gethostname = self._gh
        os.environ.pop("BIFROST_MACHINE", None)
        if self._env is not None:
            os.environ["BIFROST_MACHINE"] = self._env
        return False


def _resolve(hostname=None, env=None):
    with _As(hostname, env):
        return telemetry.resolve_machine()


# --- the defect itself: a constant, not a wrong value ---------------------------------------

def test_resolver_is_host_sensitive():
    """THE regression test. Against the pre-2026-09-09 code every one of these was
    'bifrost', so this is the assertion the old module could not satisfy."""
    answers = {_resolve(h) for h in (BIFROST_HOST, HEARTH_HOST, FORGE_HOST, POD_HOSTNAME)}
    assert len(answers) > 1, "resolve_machine() is a constant: %r" % answers


def test_each_fleet_host_resolves_to_its_own_name():
    assert _resolve(BIFROST_HOST) == "bifrost"
    assert _resolve(HEARTH_HOST) == "hearth"
    assert _resolve(FORGE_HOST) == "forge"


# --- the sentinel: never silently "bifrost" -------------------------------------------------

def test_unknown_host_is_sentinel_not_bifrost():
    assert _resolve("some-box-nobody-has-heard-of") == telemetry.MACHINE_UNKNOWN
    assert _resolve("some-box-nobody-has-heard-of") != "bifrost"


def test_pod_hostname_is_not_a_machine():
    """Inside a container gethostname() is the pod name. It must not be mistaken for a host,
    and it must not silently become 'bifrost'."""
    assert _resolve(POD_HOSTNAME) == telemetry.MACHINE_UNKNOWN


def test_empty_hostname_is_sentinel():
    assert _resolve("") == telemetry.MACHINE_UNKNOWN


def test_gethostname_failure_is_sentinel_not_a_crash():
    """Telemetry must never break the call it measures -- including at import."""
    real = socket.gethostname
    os.environ.pop("BIFROST_MACHINE", None)

    def boom():
        raise OSError("no hostname")

    socket.gethostname = boom
    try:
        assert telemetry.resolve_machine() == telemetry.MACHINE_UNKNOWN
    finally:
        socket.gethostname = real


# --- the env var, which is the only thing that works in a container -------------------------

def test_env_var_overrides_hostname():
    assert _resolve(HEARTH_HOST, env="forge") == "forge"


def test_env_var_rescues_the_container_case():
    """The pod cannot identify its node from gethostname(); the env var is how it is told."""
    assert _resolve(POD_HOSTNAME) == telemetry.MACHINE_UNKNOWN
    assert _resolve(POD_HOSTNAME, env="hearth") == "hearth"


def test_env_var_is_case_and_whitespace_tolerant():
    assert _resolve(POD_HOSTNAME, env="  HEARTH  ") == "hearth"


def test_env_var_outside_the_vocabulary_is_rejected():
    """An unvalidated env var is how a fourth vocabulary gets into the column."""
    assert _resolve(BIFROST_HOST, env="Bifrost-Prod-01") == telemetry.MACHINE_UNKNOWN


def test_empty_env_var_falls_through_to_hostname():
    assert _resolve(BIFROST_HOST, env="") == "bifrost"


# --- hostname normalization -----------------------------------------------------------------

def test_hostname_case_insensitive():
    assert _resolve(BIFROST_HOST.upper()) == "bifrost"
    assert _resolve(BIFROST_HOST.lower()) == "bifrost"


def test_hostname_fqdn_is_trimmed_to_short_name():
    assert _resolve(HEARTH_HOST + ".localdomain") == "hearth"


# --- vocabulary: fleet_config.json is the source of record ----------------------------------

def test_vocabulary_matches_fleet_config():
    """Guards against inventing a third set of machine names. Every name this module can
    produce (bar the sentinel) must already exist in fleet_config.json tiers[].machine."""
    with open(os.path.join(REPO, "fleet_config.json"), encoding="utf-8-sig") as f:
        fleet = json.load(f)
    known = {t.get("machine") for t in fleet["tiers"] if t.get("machine")}
    assert telemetry._FLEET_MACHINES <= known, (
        "telemetry knows machine names fleet_config.json does not: %s"
        % (telemetry._FLEET_MACHINES - known))


def test_cloud_is_not_a_writer_host():
    """fleet_config uses 'cloud' as a tier location. No cloud provider runs this process,
    so it must never be produced as the writer of a row."""
    assert "cloud" not in telemetry._FLEET_MACHINES
    assert _resolve(BIFROST_HOST, env="cloud") == telemetry.MACHINE_UNKNOWN


# --- hardware follows machine, and never over-claims -----------------------------------------

def test_hardware_matches_the_resolved_machine():
    assert telemetry.resolve_hardware("bifrost") == "bifrost-3950x-9070xt-rdna4"


def test_hardware_never_claims_bifrost_hardware_for_another_host():
    """fleet_config carries hardware per TIER, not per machine, so there is no source of
    record for hearth/forge here. The sentinel is correct; the old constant was not."""
    for m in ("hearth", "forge", telemetry.MACHINE_UNKNOWN, "anything-else"):
        assert telemetry.resolve_hardware(m) == telemetry.HARDWARE_UNKNOWN


# --- the models actually carry it -------------------------------------------------------------

def test_every_event_model_carries_the_resolved_machine():
    """All four writers take MACHINE as a pydantic default; if one is missed, its table keeps
    the defect while the others are fixed."""
    for model in (telemetry.InferenceEvent, telemetry.RoutingDecision,
                  telemetry.EmbedEvent, telemetry.PipelineStageEvent):
        assert model().machine == telemetry.MACHINE, model.__name__


def test_module_constants_are_resolved_not_literal():
    assert telemetry.MACHINE in telemetry._FLEET_MACHINES | {telemetry.MACHINE_UNKNOWN}
    assert telemetry.HARDWARE == telemetry.resolve_hardware(telemetry.MACHINE)


def test_inference_event_hardware_default_agrees_with_machine():
    ev = telemetry.InferenceEvent()
    assert ev.hardware == telemetry.resolve_hardware(ev.machine)


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for n, f in fns:
        try:
            f()
            print("  PASS  %s" % n)
        except Exception as e:
            bad += 1
            print("  FAIL  %s -- %s: %s" % (n, type(e).__name__, e))
    print("\n  %d passed, %d failed" % (len(fns) - bad, bad))
    sys.exit(1 if bad else 0)
