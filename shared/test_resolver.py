"""Tests for shared/resolver.py.

The load-bearing one is `test_a_redacted_placeholder_is_rejected`: the module exists
because the source archive's addresses were redacted in place, and a resolver that
happily returned "http://192.168.x.4:8089" would reproduce the exact bug it is meant
to remove. Every other test guards the error messages, because an error message that
does not name the variable is the failure mode this module is trying to prevent.

Run: python -m pytest shared/test_resolver.py --tb=line
"""

import os
import sys
import warnings
from urllib.parse import urlsplit as _urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from resolver import (  # noqa: E402
    ConfigError,
    ENDPOINTS,
    describe,
    missing,
    resolve,
    resolve_all,
)


def test_resolves_the_canonical_variable():
    env = {"RAMEAU_ROUTER_URL": "http://10.0.0.9:8089"}
    assert resolve("router", env=env) == "http://10.0.0.9:8089"


def test_strips_a_trailing_slash_so_callers_can_concatenate():
    env = {"RAMEAU_ROUTER_URL": "http://127.0.0.1:8089/"}
    assert resolve("router", env=env) == "http://127.0.0.1:8089"


def test_an_alias_is_honoured_but_deprecated():
    """The pre-release code used HEARTH_VEGA8_URL; it must keep working and complain."""
    env = {"HEARTH_VEGA8_URL": "http://127.0.0.1:11436"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve("embed", env=env) == "http://127.0.0.1:11436"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught), \
        "an alias that names a private machine must warn"


def test_the_canonical_name_wins_over_an_alias():
    env = {
        "RAMEAU_EMBED_URL": "http://127.0.0.1:1",
        "HEARTH_VEGA8_URL": "http://127.0.0.1:2",
    }
    assert resolve("embed", env=env) == "http://127.0.0.1:1"


def test_a_redacted_placeholder_is_rejected():
    """The whole reason this module exists.

    A redaction like 192.168.x.4 is not a leak and not a value. Returning it would
    push the failure down into a DNS lookup, far from the thing the operator got
    wrong. It must fail here, and say why.
    """
    env = {"RAMEAU_ROUTER_URL": "http://192.168.x.4:8089"}
    with pytest.raises(ConfigError) as err:
        resolve("router", env=env)
    msg = str(err.value)
    assert "redacted" in msg
    assert "192.168.x.4" in msg
    assert "RAMEAU_ROUTER_URL" in msg


@pytest.mark.parametrize("redacted", [
    "http://192.168.x.4:8089",     # the archive's own convention
    "http://192.168.xx.4:8089",    # a two-letter mask
    "http://192.168.x1.4:8089",    # a mask mixing a letter and a digit
    "http://192.168.2.x:8089",     # redacted last octet
    "http://x.168.2.4:8089",       # redacted first octet
    "http://192.168.x.y:8089",     # two masked octets
])
def test_every_shape_of_redaction_is_rejected(redacted):
    with pytest.raises(ConfigError) as err:
        resolve("router", env={"RAMEAU_ROUTER_URL": redacted})
    assert "redacted" in str(err.value)


@pytest.mark.parametrize("good", [
    "http://api.eu.example.com",        # four fields, no digits at all
    "http://10.infra.example.com:8089", # ALL-NUMERIC DNS LABEL in a real hostname
    "http://1.example.com:8089",        # three fields, numeric label
    "http://192.0.2.4:8089",          # a real dotted quad
    "http://10.1.2.3:80",
    "http://127.0.0.1:11434",
    "http://2026.10.27.9:8089",         # four numerics, one not octet-range
])
def test_a_legitimate_host_is_never_called_a_redaction(good):
    """The rule's cost of being wrong is refusing somebody's real endpoint and telling
    them it is archive scaffolding, so the permissive side needs the wider test set.

    `10.infra.example.com` is the case that shipped broken: the first rule asked
    whether a four-field host mixed numeric and alphabetic fields, and DNS labels may
    be all digits.
    """
    assert resolve("router", env={"RAMEAU_ROUTER_URL": good}) == good.rstrip("/")


@pytest.mark.parametrize("masked_range", [
    "http://10.x.x.x:8089",     # canonical RFC1918 shorthand
    "http://172.x.x.x:8089",
    "http://192.x.x.x:8089",
    "http://10.1.x.x:8089",     # lightly masked, caught before the floor came down too
    "http://10.0.x.x:8089",
])
def test_a_heavily_masked_private_range_is_rejected(masked_range):
    """Found in review: the octet floor of 2 ACCEPTED 10.x.x.x while refusing 10.0.x.x.

    These three shorthands are likelier in a hand-scrubbed file than the mixed-alnum
    mask the rule had just been tuned for, so the floor was the real finding.
    """
    with pytest.raises(ConfigError) as err:
        resolve("router", env={"RAMEAU_ROUTER_URL": masked_range})
    assert "redacted" in str(err.value)


def test_lowering_the_octet_floor_did_not_cost_the_control():
    """a.b.c.d has zero octets, so the floor of 1 still lets it through. This is the
    control the floor existed to protect, and it is why the change was free."""
    for ok in ("http://a.b.c.d", "http://api.eu.example.com",
               "http://node1.dc2.example.com"):
        assert resolve("router", env={"RAMEAU_ROUTER_URL": ok}) == ok


def test_x_x_x_x_is_UNREACHABLE_not_unfixed():
    """Documented boundary, not an oversight.

    `x.x.x.x` is structurally identical to `a.b.c.d` -- four single-character non-octet
    labels, zero octets -- so no shape-based rule can refuse one and accept the other.
    This test records the behaviour so a later reader sees a decision rather than a gap.
    """
    url = "http://x.x.x.x:8089"
    assert resolve("router", env={"RAMEAU_ROUTER_URL": url}) == url


@pytest.mark.parametrize("collateral", [
    # two octets, under a two-letter domain or ccTLD
    "http://8.8.8.io", "http://10.20.30.io", "http://0.1.2.ai",
    "http://1.2.t.me", "http://1.2.co.uk", "http://10.0.co.uk",
    # ONE octet, under a real two-plus-two public suffix. These entered the class
    # when the floor came down from two octets to one, and they are the reason the
    # floor and the ceiling have to be read together.
    "http://1.cs.ac.uk", "http://1.bt.co.uk", "http://2.my.id.au",
    "http://3.ab.co.nz", "http://4.xy.or.jp", "http://7.t.co.uk",
])
def test_the_price_of_the_two_character_ceiling(collateral):
    """These ARE refused, and that is the accepted cost of the <=2 mask ceiling.

    The class is numeric subdomains under a two-letter domain or ccTLD. Review could
    not name a host that exists in this shape, so no real break is claimed -- but the
    cost is real and this test exists so that anyone changing the ceiling can see
    exactly what they are trading, instead of discovering it from a bug report.

    THE FLOOR AND THE CEILING MULTIPLY, and the first version of this test documented
    only one of them. Lowering the floor from two octets to one enlarged this class
    from `<octet>.<octet>.<=2>.<=2>` to `<octet>.<=2>.<=2>.<=2>`, which lands on
    `ac.uk`, `co.uk`, `id.au`, `or.jp`, `ne.jp` and `co.nz` -- real two-plus-two
    public suffixes, not invented shapes, so this half of the class is populated by
    construction in a way `1.2.t.me` is not. The only thing between it and a real
    refusal is a numeric label in front, and numeric shard and version subdomains
    are an ordinary pattern. Still no live host named in this shape, so still no
    break claimed; the point is that whoever next touches EITHER bound sees both.
    """
    with pytest.raises(ConfigError):
        resolve("router", env={"RAMEAU_ROUTER_URL": collateral})


@pytest.mark.parametrize("safe", [
    # three fields, so the rule never engages: it requires exactly four
    "http://5.ne.jp", "http://1.go.jp", "http://9.ed.ao", "http://2.gv.at",
    # five fields, likewise out of scope
    "http://1.a.b.co.uk",
])
def test_the_price_is_BOUNDED_to_four_field_hosts(safe):
    """The bound that stops the class above from being much larger than it is.

    `_redaction_complaint` returns None unless the host has EXACTLY four
    dot-separated fields, so a numeric label under a bare two-plus-two suffix --
    `5.ne.jp`, `2.gv.at` -- is accepted, and so is anything longer. Without this
    test the price test above reads as though every numeric-label-under-a-short-
    suffix host were refused, which would overstate the cost it documents.
    """
    assert resolve("router", env={"RAMEAU_ROUTER_URL": safe}) == safe


def test_the_redaction_rule_s_documented_blind_spot():
    """`1.2.3.foo` is NOT treated as a redaction, and that is a decision.

    Three octets and a non-octet label would fit the mask shape, but a three-character
    label is far more likely to be a real hostname than a mask. This test exists so the
    boundary is recorded rather than discovered later by someone assuming it is caught.
    """
    url = "http://1.2.3.foo:8089"
    assert resolve("router", env={"RAMEAU_ROUTER_URL": url}) == url


def test_a_real_address_with_four_octets_is_NOT_mistaken_for_a_redaction():
    """Positive twin for the test above: the placeholder check must not be so eager
    that it rejects ordinary addresses."""
    for good in ("http://192.0.2.4:8089", "http://10.1.2.3:80",
                 "http://127.0.0.1:11434", "https://router.example.com"):
        assert resolve("router", env={"RAMEAU_ROUTER_URL": good}) == good.rstrip("/")


def test_a_single_letter_hostname_is_not_rejected():
    """A one-field host is not a dotted quad, so the redaction rule must leave it be."""
    assert resolve("router", env={"RAMEAU_ROUTER_URL": "http://r:8089"}) \
        == "http://r:8089"


@pytest.mark.parametrize("bad,expect", [
    ("127.0.0.1:8089", "scheme"),
    ("ftp://host/", "scheme"),
    ("http://", "host"),
])
def test_a_malformed_value_is_refused_with_the_reason(bad, expect):
    with pytest.raises(ConfigError) as err:
        resolve("router", env={"RAMEAU_ROUTER_URL": bad})
    assert expect in str(err.value)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_SET_BUT_EMPTY_variable_counts_as_UNSET(blank):
    """Compose's env_file injects every key in the file, so a blank-valued example env
    file arrives as nine set-but-empty variables. Treating that as an error would
    hard-fail a core-only deployment on the optional endpoints, which is exactly what
    `required_for` exists to prevent.
    """
    with pytest.raises(ConfigError) as err:
        resolve("kb", env={"RAMEAU_KB_URL": blank})
    msg = str(err.value)
    assert "no endpoint configured" in msg, "empty must read as unset, not as malformed"
    assert "RAMEAU_KB_URL" in msg


def test_an_empty_canonical_falls_through_to_a_set_alias():
    """The corollary: if the canonical variable is blank and a legacy alias carries a
    real value, the alias must win rather than the blank shadowing it."""
    env = {"RAMEAU_EMBED_URL": "", "HEARTH_VEGA8_URL": "http://127.0.0.1:11436"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert resolve("embed", env=env) == "http://127.0.0.1:11436"


def test_a_present_but_malformed_value_still_raises_as_malformed():
    """Positive twin for the empty-means-unset change: it must not have turned every
    bad value into a quiet 'unset'."""
    with pytest.raises(ConfigError) as err:
        resolve("kb", env={"RAMEAU_KB_URL": "not-a-url"})
    assert "set but unusable" in str(err.value)


def test_unset_names_the_variable_and_an_example():
    with pytest.raises(ConfigError) as err:
        resolve("kb", env={})
    msg = str(err.value)
    assert "RAMEAU_KB_URL" in msg, "the message must name the variable to set"
    assert ENDPOINTS["kb"].example in msg, "and show a well-formed example"
    assert "BIFROST_KB_URL" in msg, "and say which names were looked at"


def test_there_is_no_literal_fallback_anywhere_in_the_registry():
    """A guard on the module's one promise, independent of any single call site."""
    for name in ENDPOINTS:
        with pytest.raises(ConfigError):
            resolve(name, env={})


def test_no_example_is_itself_a_private_address():
    """The examples ship in .env.example, so they must be loopback or documentation
    hosts -- never anything that looks like somebody's LAN."""
    for name, spec in ENDPOINTS.items():
        assert ("127.0.0.1" in spec.example or "localhost" in spec.example
                or ".example.com" in spec.example), \
            "%s example is not an obviously-example address: %s" % (name, spec.example)


def test_no_canonical_variable_names_a_machine_or_an_accelerator():
    """The release must not ask an operator to set a variable named after a host on
    somebody else's network."""
    banned = ("HEARTH", "FORGE", "BIFROST", "VEGA", "ANVIL", "WIN10SERVER")
    for name, spec in ENDPOINTS.items():
        up = spec.env.upper()
        offenders = [b for b in banned if b in up]
        assert not offenders, "%s canonical name carries %s" % (name, offenders)


def test_an_unknown_endpoint_lists_the_known_ones():
    with pytest.raises(ConfigError) as err:
        resolve("nope", env={})
    msg = str(err.value)
    assert "unknown endpoint" in msg
    assert "router" in msg and "kb" in msg


def test_resolve_all_reports_every_failure_not_just_the_first():
    """Configuring a deployment must not be an N-round guessing game."""
    with pytest.raises(ConfigError) as err:
        resolve_all(["router", "kb", "arbiter"], env={})
    msg = str(err.value)
    assert "3 of 3" in msg
    for var in ("RAMEAU_ROUTER_URL", "RAMEAU_KB_URL", "RAMEAU_ARBITER_URL"):
        assert var in msg


def test_resolve_all_returns_the_map_when_satisfied():
    env = {"RAMEAU_ROUTER_URL": "http://127.0.0.1:1",
           "RAMEAU_KB_URL": "http://127.0.0.1:2"}
    assert resolve_all(["router", "kb"], env=env) == {
        "router": "http://127.0.0.1:1", "kb": "http://127.0.0.1:2"}


def test_missing_with_no_names_covers_the_whole_registry():
    assert set(missing(env={})) == set(ENDPOINTS)


def test_missing_with_an_EMPTY_list_means_no_endpoints():
    """Falsy is not absent. `missing(())` previously fell through to "audit everything"
    because the default was chosen with `or`."""
    assert missing((), env={}) == {}
    assert missing([], env={}) == {}


def test_resolve_all_of_nothing_returns_nothing():
    """`resolve_all(cfg.get("endpoints", []))` is an ordinary call shape. It used to
    raise "9 of 0 endpoints are not configured" -- a hard failure on a request for
    nothing, with a message that could not be true."""
    assert resolve_all([], env={}) == {}


def test_the_default_environment_is_read_per_call_not_frozen(monkeypatch):
    """A mutable default would capture the first environment the module ever saw."""
    monkeypatch.delenv("RAMEAU_ROUTER_URL", raising=False)
    with pytest.raises(ConfigError):
        resolve("router")
    monkeypatch.setenv("RAMEAU_ROUTER_URL", "http://127.0.0.1:8089")
    assert resolve("router") == "http://127.0.0.1:8089"


def test_describe_emits_every_variable_and_parses_as_env():
    text = describe()
    for spec in ENDPOINTS.values():
        assert "%s=%s" % (spec.env, spec.example) in text
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert "=" in line, "non-comment line is not KEY=value: %r" % line


def test_describe_mentions_which_endpoints_are_optional():
    text = describe()
    assert "only needed for: embeddings" in text


def test_the_loopback_example_warns_against_using_it_in_a_container():
    """The hazard is silent: loopback inside a container is the container, so the file
    would misconfigure every endpoint while looking correct."""
    text = describe("loopback")
    assert "NOT FOR A CONTAINER" in text
    assert "deploy/profiles/compose.env.example" in text


def test_the_compose_example_uses_service_names_not_loopback():
    text = describe("compose")
    assert "127.0.0.1" not in text, "loopback in an in-network profile is the bug"
    assert "localhost" not in text
    assert "RAMEAU_OLLAMA_URL=http://ollama:11434" in text
    # This assertion said 8089 until 2026-10-27, which is why the suite was green while
    # the generated file told operators to dial a closed port. A test that encodes the
    # defect is worse than a missing test: it reports the defect as the requirement.
    assert "RAMEAU_ROUTER_CHAT_URL=http://router:8080/v1/chat/completions" in text, \
        "the path must survive the host rewrite"


def test_both_profiles_define_EXACTLY_the_same_keys():
    """Two artifacts, one registry. If they ever disagree on which keys exist, one of
    them is silently incomplete -- which is the drift that generating them prevents."""
    def keys(text):
        return {ln.split("=", 1)[0] for ln in text.splitlines()
                if ln and not ln.startswith("#")}
    assert keys(describe("loopback")) == keys(describe("compose"))
    assert keys(describe("loopback")) == {s.env for s in ENDPOINTS.values()}


def test_every_registry_entry_has_a_compose_service():
    """A new endpoint must not silently fall out of the compose profile."""
    from resolver import COMPOSE_SERVICE
    assert set(COMPOSE_SERVICE) == set(ENDPOINTS)


def test_describe_refuses_an_unknown_host_mode():
    with pytest.raises(ValueError):
        describe("kubernetes")


# ---------------------------------------------------------------------------
# Ports: the registry against itself, and against the artifacts that listen.
#
# The defect these guard was not a typo. The compose example was generated by reusing
# the port out of each loopback example, so it inherited the private deployment's
# PUBLISHED ports -- numbers that are real on a host and meaningless on a container
# network. Nothing could catch it, because every artifact agreed with the registry and
# the registry was the thing that was wrong.
# ---------------------------------------------------------------------------


def _release_root():
    """The repo root: this file is in shared/, one level down."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_artifact(rel):
    """Read a file elsewhere in the repo, FAILING if it is absent.

    Deliberately not a skip. A cross-artifact assertion that stops running when its
    other half moves is worse than no assertion at all, because the suite still reports
    green -- and the whole point here is that the registry's claims about the Dockerfile
    and the compose file cannot be taken on trust.
    """
    path = os.path.join(_release_root(), rel.replace("/", os.sep))
    assert os.path.exists(path), (
        "%s is missing, so the registry's claim about it cannot be checked" % rel)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_every_endpoint_declares_the_port_it_listens_on():
    """Without it the compose value has no port at all, so a new endpoint must set it."""
    undeclared = sorted(n for n, s in ENDPOINTS.items() if s.listens_on is None)
    assert undeclared == [], (
        "these endpoints have no listens_on, so their in-network address cannot be "
        "derived: %s" % ", ".join(undeclared))


def test_endpoints_sharing_a_compose_service_agree_on_the_port():
    """Three variables name the router. One process cannot bind three ports."""
    from resolver import COMPOSE_SERVICE
    by_service = {}
    for name, service in COMPOSE_SERVICE.items():
        by_service.setdefault(service, {})[name] = ENDPOINTS[name].listens_on
    for service, ports in sorted(by_service.items()):
        assert len(set(ports.values())) == 1, (
            "compose service %r is addressed on more than one port %r; at least one of "
            "those endpoints is wrong" % (service, ports))


def test_the_host_example_port_matches_listens_on_unless_declared():
    """The default is that the two agree, so a divergence has to be written down."""
    from urllib.parse import urlsplit

    from resolver import HOST_PORT_DIFFERS
    for name, spec in sorted(ENDPOINTS.items()):
        if name in HOST_PORT_DIFFERS:
            continue
        assert urlsplit(spec.example).port == spec.listens_on, (
            "%s: the host example uses %s but the unit listens on %s. If that is real, "
            "declare it in HOST_PORT_DIFFERS with the reason; if it is not, one of the "
            "two numbers is a mistake"
            % (name, urlsplit(spec.example).port, spec.listens_on))


def test_a_declared_divergence_must_actually_diverge():
    """The control for the test above: HOST_PORT_DIFFERS is an exemption list, and a
    stale entry in an exemption list widens it silently. An endpoint whose ports have
    since been made to agree must drop out of the list rather than keep its waiver."""
    from urllib.parse import urlsplit

    from resolver import HOST_PORT_DIFFERS
    assert set(HOST_PORT_DIFFERS) <= set(ENDPOINTS), (
        "HOST_PORT_DIFFERS names an endpoint that no longer exists: %s"
        % sorted(set(HOST_PORT_DIFFERS) - set(ENDPOINTS)))
    for name, declared in sorted(HOST_PORT_DIFFERS.items()):
        spec = ENDPOINTS[name]
        assert urlsplit(spec.example).port != spec.listens_on, (
            "%s is exempted from the port check but its two ports agree, so the waiver "
            "is stale and is now hiding any future divergence" % name)
        assert len(declared.reason) > 40, (
            "%s's waiver needs a reason someone can audit, not a placeholder" % name)


def test_a_declared_divergence_pins_the_host_port_it_declares():
    """Keyed by name alone the waiver said "kb may diverge", never "kb diverges by
    exactly this much" -- so an edit to the host port inside `example` had nothing to
    disagree with. The number is repeated as an int here, so now it does."""
    from urllib.parse import urlsplit

    from resolver import HOST_PORT_DIFFERS
    for name, declared in sorted(HOST_PORT_DIFFERS.items()):
        assert urlsplit(ENDPOINTS[name].example).port == declared.port, (
            "%s's example uses port %s but HOST_PORT_DIFFERS declares %d; one of the two "
            "was edited without the other"
            % (name, urlsplit(ENDPOINTS[name].example).port, declared.port))


# ---------------------------------------------------------------------------
# Provenance. These exist because of a reviewer's finding on the first version of the
# port fix: only `router` had an artifact in this repo that could contradict its
# `listens_on`. For every other endpoint the one test that could catch a wrong value was
# the generated-files comparison -- and both sides of it derive from the registry, so
# editing `listens_on` and then running `--write-examples` put the suite back to green
# while shipping a port nothing binds. The guard dissolved on the step the docs
# prescribe, which is the same defect class the whole change set out to remove.
# ---------------------------------------------------------------------------


def test_every_listens_on_has_a_provenance_record():
    from resolver import PROVENANCE
    assert set(PROVENANCE) == set(ENDPOINTS), (
        "PROVENANCE and ENDPOINTS disagree about which endpoints exist: only in "
        "PROVENANCE %s, only in ENDPOINTS %s"
        % (sorted(set(PROVENANCE) - set(ENDPOINTS)),
           sorted(set(ENDPOINTS) - set(PROVENANCE))))


def test_provenance_agrees_with_the_registry():
    """The second, independent copy of the number. It cannot prove what a service binds
    -- this repo cannot reach any of them -- but it makes a ONE-SIDED edit fail, and a
    two-sided edit has to restate the artifact it came from."""
    from resolver import PROVENANCE
    for name, spec in sorted(ENDPOINTS.items()):
        record = PROVENANCE[name]
        assert record.port == spec.listens_on, (
            "%s: the registry says it listens on %s, PROVENANCE says %d (%s). Edit both "
            "and cite the artifact, or neither"
            % (name, spec.listens_on, record.port, record.source))
        assert len(record.source) > 20, (
            "%s's provenance has to name an artifact a reviewer can open" % name)


def test_a_shipped_image_is_checked_against_its_provenance():
    """Where the artifact ships here, the anchor is a real cross-check rather than a
    second opinion: the file must exist and must contain the port."""
    from resolver import PROVENANCE
    checked = 0
    for name, record in sorted(PROVENANCE.items()):
        if record.in_repo is None:
            continue
        text = _read_artifact(record.in_repo)
        assert str(record.port) in text, (
            "%s cites %s for port %d, and that file does not contain the number"
            % (name, record.in_repo, record.port))
        checked += 1
    assert checked > 0, (
        "no endpoint is anchored to an artifact in this repo, so this test proved "
        "nothing -- if that is now true, it is a finding, not a pass")


def test_every_service_whose_image_ships_here_is_anchored_to_it():
    """The control that keeps the gap from reopening. Today only the router's image is in
    deploy/images/, so only the router can be checked against a Dockerfile. The moment
    another service's image lands, every endpoint on that service must cite it -- which
    is a test that gets STRICTER as the release grows, rather than one that has to be
    remembered."""
    import glob

    from resolver import COMPOSE_SERVICE, PROVENANCE
    pattern = os.path.join(_release_root(), "deploy", "images", "*", "Dockerfile")
    for path in sorted(glob.glob(pattern)):
        service = os.path.basename(os.path.dirname(path))
        rel = "deploy/images/%s/Dockerfile" % service
        on_service = [n for n, s in COMPOSE_SERVICE.items() if s == service]
        if not on_service:
            continue  # an image with no endpoint, e.g. the portal
        for name in sorted(on_service):
            assert PROVENANCE[name].in_repo == rel, (
                "%s's image ships at %s but endpoint %r cites %r instead. An artifact "
                "in this repo can be checked; a citation to one outside it cannot"
                % (service, rel, name, PROVENANCE[name].in_repo))


def test_two_different_services_do_not_share_one_loopback_address():
    """A host has ONE port namespace, so two services cannot both answer on one
    host:port. This is the invariant that makes HOST_PORT_DIFFERS necessary rather than
    merely tolerated: the first attempt at this fix set kb's and the broadcaster's
    examples to the port they both bind, 8090, which made the loopback file address two
    different services at one and the same URL. The router's three variables are exempt
    because they are one service reached by three paths."""
    from urllib.parse import urlsplit

    from resolver import COMPOSE_SERVICE
    seen = {}
    for name, spec in sorted(ENDPOINTS.items()):
        parts = urlsplit(spec.example)
        authority = (parts.scheme, parts.hostname, parts.port)
        other = seen.get(authority)
        if other is not None:
            assert COMPOSE_SERVICE[name] == COMPOSE_SERVICE[other], (
                "%s and %s are different services but share the host address %s://%s:%s; "
                "on one host only one of them can be listening there"
                % (other, name, parts.scheme, parts.hostname, parts.port))
        else:
            seen[authority] = name


def test_the_compose_example_addresses_the_listen_port():
    """The generated value for every endpoint, checked against listens_on rather than
    against a hand-written expected string."""
    from resolver import COMPOSE_SERVICE
    text = describe("compose")
    for name, spec in sorted(ENDPOINTS.items()):
        want = "%s=http://%s:%d" % (spec.env, COMPOSE_SERVICE[name], spec.listens_on)
        assert want in text, "compose example does not contain %r" % want


def test_the_published_ports_do_not_come_back_in_the_compose_example():
    """Named regression. Each of these was in the generated file on 2026-10-27."""
    text = describe("compose")
    for wrong in (":8089", ":8091", ":8092", ":11436", ":3110", "/router/status"):
        assert wrong not in text, (
            "%r is a host-side value and has no meaning on a container network" % wrong)


def test_the_health_probe_target_is_not_the_unfalsifiable_route():
    """Named regression: this one value has been wrong twice, both times in a way that
    reads green. First `/router/status`, which is the prefix the portal's nginx proxies
    under rather than a route the application serves -- so a portal fault answered for
    the router. Then `/status`, which returns "router": "online" unconditionally, makes
    no backend calls, and so cannot report not-ready; it also discloses which cloud API
    keys are set. The literal is spelled out here rather than read from the registry, so
    a one-sided edit fails."""
    from urllib.parse import urlsplit
    path = urlsplit(ENDPOINTS["router_health"].example).path
    assert path == "/health", (
        "the readiness probe target is %r. /status cannot report not-ready and /livez is "
        "liveness only; /health is the route that probes the backends" % path)


def test_the_router_dockerfile_and_the_registry_agree_on_the_port():
    """The cross-artifact assertion: the registry says what the router listens on, and
    the Dockerfile is the thing that makes it true. Nothing else connects them."""
    import re
    text = _read_artifact("deploy/images/router/Dockerfile")
    want = str(ENDPOINTS["router"].listens_on)
    exposed = re.findall(r"(?m)^EXPOSE\s+(\d+)", text)
    served = re.findall(r'"--port"\s*,\s*"(\d+)"', text)
    assert exposed == [want], (
        "Dockerfile EXPOSEs %r but the registry says the router listens on %s"
        % (exposed, want))
    assert served == [want], (
        "Dockerfile starts uvicorn on %r but the registry says %s" % (served, want))


def test_the_compose_file_and_the_registry_agree_on_the_router_port():
    """compose.yaml hardcodes the three router URLs rather than reading the example
    file, so it is a third place the number lives and a third place it can drift."""
    import re
    text = _read_artifact("deploy/compose.yaml")
    want = ENDPOINTS["router"].listens_on
    for key in ("router", "router_chat", "router_health"):
        spec = ENDPOINTS[key]
        # The WHOLE value, path included. A prefix match on the port let compose.yaml
        # keep `/status` for the health URL after the registry moved to `/health` --
        # the two artifacts disagreed and the test read green, because the only thing
        # it compared was the number it was written to compare.
        whole = "%s: http://router:%d%s" % (
            spec.env, want, _urlsplit(spec.example).path)
        assert whole in text, (
            "compose.yaml does not set %s to %r" % (spec.env, whole.split(": ", 1)[1]))
    container_side = re.findall(r"RAMEAU_ROUTER_PUBLISH[^\"]*\}:(\d+)\"", text)
    assert container_side == [str(want)], (
        "the published mapping's container side is %r, not the port the router binds "
        "(%d) -- publishing to a closed port answers nothing" % (container_side, want))


def test_the_checked_in_example_files_are_what_describe_generates():
    """A generated file that is hand-edited, or left stale after a registry change, is
    no longer evidence of anything the registry says. This is the only test that can
    tell -- every other one here reads describe() rather than the file."""
    for rel, mode, flag in ((".env.example", "loopback", "--env-example"),
                            ("deploy/profiles/compose.env.example", "compose",
                             "--compose-example")):
        on_disk = _read_artifact(rel).replace("\r\n", "\n")
        assert on_disk == describe(mode), (
            "%s is stale; regenerate it with `python shared/resolver.py %s`"
            % (rel, flag))
