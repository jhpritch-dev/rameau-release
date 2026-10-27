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
    "http://8.8.8.io", "http://10.20.30.io", "http://0.1.2.ai",
    "http://1.2.t.me", "http://1.2.co.uk", "http://10.0.co.uk",
])
def test_the_price_of_the_two_character_ceiling(collateral):
    """These ARE refused, and that is the accepted cost of the <=2 mask ceiling.

    The class is numeric subdomains under a two-letter domain or ccTLD. Review could
    not name a host that exists in this shape, so no real break is claimed -- but the
    cost is real and this test exists so that anyone changing the ceiling can see
    exactly what they are trading, instead of discovering it from a bug report.
    """
    with pytest.raises(ConfigError):
        resolve("router", env={"RAMEAU_ROUTER_URL": collateral})


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
    assert "RAMEAU_ROUTER_CHAT_URL=http://router:8089/v1/chat/completions" in text, \
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
