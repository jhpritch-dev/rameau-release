"""Endpoint resolution for RAMEAU.

Every endpoint in the registry below is resolved here, from the environment, with no
fallback to a literal address. The module exists because of a specific failure mode found
in the source archive and recorded in RELEASE-SCOPE.md: the private deployment's addresses
had been redacted *in place*, leaving code like

    os.getenv("SOME_URL", "http://192.168.x.4:8089")

Fourteen such call sites were measured across eight files. None of them leaks an address
any more -- but none of them works either. A user who does not set the variable gets a
DNS failure against a hostname that cannot exist, several layers below the thing they
actually got wrong, which is that they did not configure an endpoint.

So the rule here is: **no fallback to a literal address, ever.** An unset endpoint raises
at the point of resolution with the variable's name and an example value in the message.
Failing immediately and legibly is worth more than starting and misbehaving.

WHAT THIS MODULE DOES *NOT* COVER, stated because the docstring said "and nowhere else"
until 2026-11-02 and that was too strong. The session assembling the release reports that
`fleet_config.json` -- bind-mounted by deploy/compose.yaml and re-read on mtime change --
carries a `tiers[].base_url` per tier, each a full ``scheme://host:port``. Those are
addresses the router talks to, and they reach it without passing through the environment
or through this registry. Nothing here can see that file, so this is a limit on the claim
rather than a defect in the code: the registry governs the nine endpoints it lists, and is
not the only address surface the router has. RELEASE-SCOPE.md carries it as an assembly
obligation.

Stdlib only, by design: this is imported by nearly everything.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, NamedTuple, Optional
from urllib.parse import urlsplit

__all__ = [
    "ConfigError",
    "Endpoint",
    "ENDPOINTS",
    "resolve",
    "resolve_all",
    "missing",
    "describe",
]


class ConfigError(RuntimeError):
    """An endpoint was needed and the environment does not define it."""


class Endpoint(NamedTuple):
    """One service address the router may need.

    env     -- the canonical environment variable.
    purpose -- what breaks when it is absent, in the operator's terms.
    example -- a value that is obviously an example and obviously well-formed.
    aliases -- variable names kept for compatibility with the pre-release code.
               Read in order after `env`; the first one set wins.
    required_for -- a short tag naming which part of the system needs it, so a
               deployment that uses only the core can tell what it may ignore.
    listens_on -- the port the unit itself LISTENS ON, which is the port to use when
               addressing it across a container network. Every value is anchored in
               PROVENANCE below; read that before trusting one.

    `listens_on` is not a duplicate of the port in `example`, and the difference is the
    whole reason it exists. `example` is a HOST-side value: it may go through a published
    port, a proxy or a port mapping. `listens_on` is what the process binds. The compose
    example is generated from `listens_on`, because inside a container network there is no
    mapping to go through -- you reach the service on the port it binds, by service name.

    Before this field existed the compose example reused the port out of `example`, and so
    inherited the private deployment's published ports: it told operators to address the
    router on 8089 when the container binds 8080, the knowledge base on 8091 and the
    broadcaster on 8092 when both bind 8090. Those URLs resolve to a running container on a
    closed port, so they fail at FIRST USE rather than at startup -- and the router answers
    `/status` with "online" while they do, which is the class of failure this whole module
    exists to remove.
    """

    env: str
    purpose: str
    example: str
    aliases: tuple = ()
    required_for: str = "core"
    listens_on: Optional[int] = None


# The registry below was built by scanning the source archive's own call sites rather
# than from memory, so the alias names are the ones the code actually used. Where an
# old name embedded a private machine name (HEARTH_*) or a GPU model (VEGA8), the
# canonical name is neutral and the old one is kept only as an alias.
ENDPOINTS: Dict[str, Endpoint] = {
    # The router binds 8080 -- `EXPOSE 8080` and `--port 8080` in
    # deploy/images/router/Dockerfile, which a test asserts against this number, so for
    # THIS endpoint the registry cannot drift from the thing that actually listens.
    # That holds for the router because its image ships here; see PROVENANCE below for
    # what anchors the other endpoints and how much weaker that anchor is. 8089 was the
    # private deployment's PUBLISHED port and is not a property of the software.
    "router": Endpoint(
        env="RAMEAU_ROUTER_URL",
        purpose="the router's own OpenAI-compatible base; clients and self-checks use it",
        example="http://127.0.0.1:8080",
        aliases=("BIFROST_ROUTER_BASE", "BIFROST_A2A_PUBLIC_URL"),
        listens_on=8080,
    ),
    "router_chat": Endpoint(
        env="RAMEAU_ROUTER_CHAT_URL",
        purpose="full chat-completions URL, when a client needs the path and not the base",
        example="http://127.0.0.1:8080/v1/chat/completions",
        aliases=("BIFROST_ROUTER_URL",),
        listens_on=8080,
    ),
    # The PATH here was wrong as well as the port, and then wrong a second way.
    #
    # `/router/status` is the prefix the portal's nginx proxies under, not a route the
    # application serves -- and probing the router through the portal makes a portal
    # fault read as a router fault, which is the wrong answer to "is the router ready".
    #
    # The first correction pointed this at `/status`, which contradicted the reason the
    # field exists: `/status` returns a literal "router": "online" and makes no backend
    # calls, so it cannot report not-ready. It is also unauthenticated and discloses
    # which cloud API keys are set, which is not something to nominate as the documented
    # probe target. `/health` is the readiness route -- it probes the classifier and the
    # embedding backend and reports each up/down.
    #
    # READ THE BODY, NOT THE STATUS CODE. `/health` answers 200 whether those
    # dependencies are up or down; the verdict is the JSON. A caller that treats the
    # status code as the answer -- `curl -f`, or a container healthcheck -- reads green
    # while generation is failing, which is the exact class of failure this module
    # exists to remove.
    #
    # This comment claimed until 2026-11-02 that the application serves `/livez` for
    # liveness. A measurement of the router source ruled into scope counts ZERO
    # occurrences of `livez` or `readyz`, so that claim is suspended: it describes an
    # artefact this release may not ship. It does not affect anything this module does,
    # and `/health` is still the right probe target for the reason above -- the
    # `example` here is unchanged and the test asserting it is unaffected. The claim is
    # inventoried in RELEASE-SCOPE.md, because the remedy depends on a scope ruling that
    # has not been made.
    "router_health": Endpoint(
        env="RAMEAU_ROUTER_HEALTH_URL",
        purpose="readiness probe target; reports backend reachability in its BODY",
        example="http://127.0.0.1:8080/health",
        aliases=("BIFROST_ROUTER_HEALTH_URL",),
        listens_on=8080,
    ),
    "ollama": Endpoint(
        env="RAMEAU_OLLAMA_URL",
        purpose="the local inference backend serving the generation tiers",
        example="http://127.0.0.1:11434",
        aliases=("BIFROST_OLLAMA_URL",),
        listens_on=11434,
    ),
    "embed": Endpoint(
        env="RAMEAU_EMBED_URL",
        purpose="the embedding endpoint; kept separate so embeddings never contend "
                "with generation on one accelerator",
        example="http://127.0.0.1:11436",
        # HEARTH_VEGA8_URL named a specific machine and a specific iGPU. Neither
        # belongs in a public variable name, so it survives only as an alias.
        #
        # The two BIFROST_*_EMBED_URL names are per-consumer overrides the archive grew
        # independently -- the routing cache and the autopilot graph each read their own
        # variable rather than one shared name. Both are recorded here as aliases rather
        # than as endpoints of their own, because this is NOT a new service: each
        # literal they defaulted to was the embedding port, so the registry is recording
        # what the code already pointed at. An operator who sets only RAMEAU_EMBED_URL
        # now configures all three call sites, which is the behavior the registry is
        # for; an operator still carrying either old name keeps working.
        aliases=("HEARTH_VEGA8_URL", "BIFROST_ROUTING_CACHE_EMBED_URL",
                 "BIFROST_AUTOPILOT_EMBED_URL"),
        required_for="embeddings",
        # The one endpoint whose host and in-network ports legitimately differ -- see
        # HOST_PORT_DIFFERS below.
        listens_on=11434,
    ),
    # Binds 8090, published on 8091. Both numbers are real and neither is a substitute
    # for the other -- see HOST_PORT_DIFFERS.
    "kb": Endpoint(
        env="RAMEAU_KB_URL",
        purpose="the retrieval service backing knowledge-base lookups",
        example="http://127.0.0.1:8091",
        aliases=("BIFROST_KB_URL",),
        required_for="retrieval",
        listens_on=8090,
    ),
    # 8082 in-network and published, per the same descriptor: this one was already right,
    # which is why it is the control that the correction is not a blanket rewrite.
    "arbiter": Endpoint(
        env="RAMEAU_ARBITER_URL",
        purpose="the adjudication service for multi-candidate tier output",
        example="http://127.0.0.1:8082",
        aliases=("ARBITER_URL",),
        required_for="arbitration",
        listens_on=8082,
    ),
    # Binds 8090 as well, which is why it had to be published on a different port.
    "broadcaster": Endpoint(
        env="RAMEAU_BROADCASTER_URL",
        purpose="fan-out of request/response events to observers",
        example="http://127.0.0.1:8092",
        aliases=("BROADCASTER_URL",),
        required_for="observability",
        listens_on=8090,
    ),
    # 8080, not 8888. searxng's image binds 8080 and the deployment this example came
    # from publishes it as `8888:8080`. I had this wrong in the first pass of the very
    # fix that introduced listens_on -- I asserted 8888 from "searxng's default" without
    # opening an artifact, which is precisely the unanchored-number defect PROVENANCE
    # below exists to make impossible.
    "search": Endpoint(
        env="RAMEAU_SEARCH_URL",
        purpose="the self-hosted metasearch endpoint used by the research path",
        example="http://127.0.0.1:8888",
        aliases=("SEARXNG_URL",),
        required_for="search",
        listens_on=8080,
    ),
    # Added 2026-11-12. The portal was the one service this release SHIPS -- image,
    # compose service and source -- that had no entry here, and the gap had a cost: the
    # dev server's vite config carried a literal LAN address as its proxy target,
    # because there was no name to read instead. It is the only endpoint whose consumer
    # is JavaScript rather than Python, so nothing here resolves it; the registry's job
    # for this one is to OWN THE NAME, so the JS side and an operator agree on it.
    #
    # `aliases` is deliberately empty, and that is a statement rather than an omission:
    # the archive had no variable for this: the address was a hardcoded const. There is
    # no old name to stay compatible with.
    "portal": Endpoint(
        env="RAMEAU_PORTAL_URL",
        purpose="the portal's nginx, which serves the SPA and reverse-proxies the "
                "service routes the browser calls by relative path",
        example="http://127.0.0.1:13110",
        required_for="portal",
        listens_on=80,
    ),
}


class HostPort(NamedTuple):
    """A declared divergence between an endpoint's host-side and in-network ports.

    `port` repeats the host-side number as an int so the declaration says "diverges by
    exactly this much" rather than the weaker "may diverge". Keyed by name alone, the
    waiver exempted an endpoint from the port check without pinning either number, so an
    edit to the host port inside `example` had nothing to disagree with.
    """

    port: int
    reason: str


# Endpoints whose HOST-side example port legitimately differs from `listens_on`. A test
# requires every other endpoint's two ports to agree, so a new divergence has to be
# declared here and justified rather than appearing silently -- which is how the wrong
# compose ports got in.
#
# Every entry is one phenomenon: a host's loopback is a single port namespace shared by
# every service on it, while each container gets its own. Services that bind the same
# port therefore have to be published on different ones, and the published number is
# what a process ON THE HOST must dial -- so it is the right value for the loopback
# example and the wrong one for the compose example. Erasing the divergence in either
# direction breaks the other file: setting these examples to `listens_on` would have the
# loopback file address kb and broadcaster at one and the same URL.
#
# Note what these host numbers ARE. Unlike `listens_on`, they are not claims about what
# any software binds -- they are example values, chosen to be plausible and not to
# collide, and `example`'s own docstring says as much. They are pinned here so that a
# one-sided edit fails, not because a particular host must use them.
HOST_PORT_DIFFERS: Dict[str, HostPort] = {
    "embed": HostPort(11436,
                      "ollama's default is 11434 and two instances on one host cannot "
                      "both bind it, so a second instance needs a distinct host port. "
                      "In its own container there is no contention and it binds 11434."),
    "kb": HostPort(8091, "binds 8090, the same port as the broadcaster, so the two "
                         "cannot share one host port."),
    "broadcaster": HostPort(8092, "binds 8090, the same port as the knowledge base, so "
                                  "the two cannot share one host port."),
    "search": HostPort(8888, "searxng's image binds 8080, which the router also binds, "
                             "so on one host the two cannot both have it."),
    "portal": HostPort(13110,
                       "nginx binds 80 in its own container. On a host, 80 is the "
                       "default HTTP port and is not a port a deployment can assume is "
                       "free, so this one is published high: deploy/compose.yaml "
                       "publishes `${RAMEAU_PORTAL_PUBLISH:-127.0.0.1:13110}:80`, and "
                       "13110 is that default."),
}


class Provenance(NamedTuple):
    """Where a `listens_on` value came from, and whether this repo can check it.

    source  -- the artifact and the line in it that sets the port, in words.
    port    -- the number again, as an int.
    in_repo -- repo-relative path, when that artifact SHIPS HERE; None when it does not.
    """

    source: str
    port: int
    in_repo: Optional[str] = None


# The anchor for every `listens_on`. This exists because of a defect a reviewer found in
# the first version of this fix, and the defect is worth stating because the shape of the
# cure follows from it:
#
#   Only `router` had an artifact in this repo to contradict it. For the other five the
#   sole test that could catch a wrong `listens_on` was the one comparing the generated
#   example files to the registry -- and BOTH SIDES of that comparison derive from the
#   registry. So: edit `listens_on`, run `--write-examples` (the command this repo's own
#   CI failure message tells you to run), and the suite goes green again while shipping a
#   port nothing binds. The guard dissolved on exactly the step the docs prescribe.
#
# It is not a measurement -- this repo cannot reach the services. What it does is make a
# silent ONE-SIDED edit impossible and name the artifact a reviewer should open, and for
# any service whose image ships here it is checked against that image.
#
# THE LIMIT, measured rather than reasoned about, and recorded here because the harness
# that measured it is a scratch file that will not survive: a two-sided edit -- changing
# `listens_on` and this table's port together -- is caught, UNTIL the example files are
# regenerated. After `--write-examples` it goes green and ships the forged port. Both
# halves of the cross-check moved together, so from inside this repo that is
# indistinguishable from a real correction, and no test here can tell the difference.
# The two-sided edit alone is caught; the two-sided edit followed by the documented
# regeneration step is not. What closes it is opening the cited artifact, which is why
# each entry names one.
#
# Every number below was read out of the artifact named, in the private archive, at the
# time of this change. `search` is the proof the problem was real rather than
# theoretical: the first pass of this fix asserted 8888 from recollection of searxng's
# default, and the artifact says the image binds 8080 and the deployment published
# `8888:8080`.
PROVENANCE: Dict[str, Provenance] = {
    "router": Provenance("EXPOSE 8080 and `--port 8080` in this repo's own router image",
                         8080, "deploy/images/router/Dockerfile"),
    "router_chat": Provenance("same process as `router`", 8080,
                              "deploy/images/router/Dockerfile"),
    "router_health": Provenance("same process as `router`", 8080,
                                "deploy/images/router/Dockerfile"),
    "kb": Provenance("the knowledge-base service's Dockerfile: `--port 8090` on its "
                     "uvicorn CMD, with no EXPOSE line", 8090),
    "broadcaster": Provenance("the broadcaster's Dockerfile (EXPOSE 8090 and `--port "
                              "8090`), and independently its Kubernetes Deployment "
                              "containerPort 8090 with both Services at "
                              "port/targetPort 8090", 8090),
    "arbiter": Provenance("the arbiter's Dockerfile (EXPOSE 8082 and `--port 8082`), and "
                          "independently its Deployment containerPort 8082", 8082),
    # The `source` below is the whole of the evidence, and it is checkable: `8888:8080`
    # is HOST:CONTAINER, unambiguously, so a reader who opens that file can confirm 8080
    # without knowing anything else.
    #
    # Separately, and NOT as evidence: loopback 8080 was already held by another service
    # on the host this example came from, so searxng could not have been published there.
    # That is why the divergence had to exist. It is deliberately not in `source`,
    # because nobody can verify it from this repository -- it is the reason, not the
    # anchor, and writing it where it reads as checkable would be worse than leaving it
    # out. Which services held 8080 is in the private record; naming them here would put
    # an inventory of someone's host in a public file and would add nothing auditable.
    "search": Provenance("the searxng image binds 8080: the deployment this example came "
                         "from publishes it as `8888:8080`, i.e. host 8888 to container "
                         "8080", 8080),
    # The weakest of the eight, and labeled rather than dressed up: the private
    # deployment runs its embedding instance natively on a host rather than in a
    # container, so there is no artifact that sets a container port. 11434 is ollama's
    # default bind port, which is what an embed CONTAINER would use.
    "embed": Provenance("ollama's default bind port; no container artifact exists for "
                        "this service, so this is the recipe's assumption, not a "
                        "measurement", 11434),
    "ollama": Provenance("ollama's default bind port", 11434),
    # The second of the eight that is checkable from inside this repository, and the
    # strongest kind: the image ships here, so the test that reads the cited file can
    # confirm the number rather than take it on report.
    "portal": Provenance("EXPOSE 80 in this repo's own portal image", 80,
                         "deploy/images/portal/Dockerfile"),
}


def _is_octet(field: str) -> bool:
    """True for a field that could be an IPv4 octet: 1-3 digits, value 0-255."""
    return field.isdigit() and len(field) <= 3 and int(field) <= 255


def _redaction_complaint(host: str) -> Optional[str]:
    """Return a complaint when `host` looks like a MASKED IPv4 address.

    This is the failure the module exists for: the source archive redacted addresses
    in place, leaving values like 192.168.x.4 that are neither a leak nor usable.

    The discriminator is OCTETS, not "numeric versus alphabetic". An earlier version
    asked whether a four-field host mixed numeric and alphabetic fields, which
    rejected `10.infra.example.com` -- a perfectly valid FQDN, since DNS labels may be
    all digits -- and told the operator their own address was archive scaffolding.

    A masked address keeps some of its octets and replaces the rest with short mask
    tokens. So: four fields, at least ONE real octet, at least one field that is not an
    octet, and every such field at most two characters long.

    The octet floor was 2 until review found it let the heavily-masked cases through
    while catching the lightly-masked ones: `10.x.x.x`, `172.x.x.x` and `192.x.x.x` --
    the canonical shorthands for the RFC1918 ranges, and far likelier in a
    hand-scrubbed file than the `192.168.x1.4` the rule was tuned for -- were ACCEPTED,
    while `10.0.x.x` was refused. A floor of 1 catches all three and still accepts
    `a.b.c.d`, which has zero octets and is the control the floor was there to protect,
    so the floor came down for free.

    Two boundaries this deliberately does NOT catch. Both are decisions, recorded so
    they read as such rather than as oversights:

    `1.2.3.foo` -- three octets and a three-character label. More likely somebody's
    hostname than a mask, and refusing a live endpoint is the worse error.

    `x.x.x.x` -- structurally identical to `a.b.c.d`: four single-character non-octet
    labels, zero octets. No shape-based rule can refuse one and accept the other, so
    this is unreachable rather than unfixed. Catching it would need a mask alphabet,
    which trades a named cost for an unmeasured one.

    And the price of the two-character ceiling, stated plainly because it was picked
    rather than measured: a four-label host whose non-octet labels are ALL at most two
    characters, with at least one octet, is refused. The class is numeric subdomains
    under a two-letter domain or ccTLD -- `8.8.8.io`, `10.20.30.io`, `1.2.co.uk`,
    `1.2.t.me`. Review could not name a host that actually exists in that shape, so no
    real break is claimed; `co.uk` is what keeps the class from being purely exotic.
    The error it produces names the host and is actionable, which is why the cost is
    judged worth paying.
    """
    fields = host.split(".")
    if len(fields) != 4:
        return None
    octets = [f for f in fields if _is_octet(f)]
    masked = [f for f in fields if not _is_octet(f)]
    if len(octets) >= 1 and masked and all(len(f) <= 2 for f in masked):
        return ("it still carries a redacted address (%r) -- this is a placeholder "
                "from the source archive, not a working value" % host)
    return None


def _looks_like_a_url(value: str) -> Optional[str]:
    """Return a complaint about `value`, or None when it is usable.

    Deliberately shallow: this catches the mistakes that actually happen -- a bare
    host, a leftover redaction, a trailing slash -- and does not try to validate
    hostnames, which is the resolver's business to pass through, not to police.

    An empty value is NOT this function's business: `resolve` treats empty as unset.
    """
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return "it has no http:// or https:// scheme"
    if not parts.netloc:
        return "it has no host"
    return _redaction_complaint(parts.hostname or "")


def resolve(name: str, env: Optional[Dict[str, str]] = None) -> str:
    """Resolve one endpoint by registry name. Raise ConfigError if unusable.

    `env` is injected rather than read at import time so that tests and callers can
    resolve against a specific mapping; a default of None is late-bound to os.environ
    on every call, because a mutable default would freeze the first environment seen.
    """
    try:
        spec = ENDPOINTS[name]
    except KeyError:
        known = ", ".join(sorted(ENDPOINTS))
        raise ConfigError(
            "unknown endpoint %r. Known endpoints: %s" % (name, known)) from None

    src = os.environ if env is None else env

    for var in (spec.env,) + tuple(spec.aliases):
        raw = src.get(var)
        # EMPTY MEANS UNSET, deliberately. Compose's `env_file` injects every key in
        # the file, so a blank-valued example env file arrives as a set-but-empty
        # variable for all nine endpoints. Treating that as an error would hard-fail a
        # core-only deployment on `kb`, `arbiter`, `broadcaster` and `search` -- the
        # very endpoints whose `required_for` tags exist so they can be left out. An
        # empty value therefore falls through to the next alias and then to the
        # "not configured" error, which names the variable. A value that is present
        # but malformed is a different mistake and still raises as such.
        if raw is None or not raw.strip():
            continue
        complaint = _looks_like_a_url(raw)
        if complaint is not None:
            raise ConfigError(
                "%s is set but unusable: %s.\n"
                "  needed for: %s\n"
                "  example:    %s=%s"
                % (var, complaint, spec.purpose, spec.env, spec.example))
        if var != spec.env:
            # Honored, but say so once: the alias names a private machine or a
            # specific accelerator and will not be read forever.
            _warn_alias(var, spec)
        return raw.rstrip("/")

    tried = ", ".join((spec.env,) + tuple(spec.aliases))
    raise ConfigError(
        "no endpoint configured for %r.\n"
        "  needed for: %s\n"
        "  set:        %s=%s\n"
        "  (looked at: %s)"
        % (name, spec.purpose, spec.env, spec.example, tried))


_warned: set = set()


def _warn_alias(var: str, spec: Endpoint) -> None:
    if var in _warned:
        return
    _warned.add(var)
    import warnings
    warnings.warn(
        "%s is a compatibility alias and may be removed; prefer %s"
        % (var, spec.env),
        DeprecationWarning,
        stacklevel=3,
    )


def missing(names: Optional[Iterable[str]] = None,
            env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Report which of `names` cannot be resolved, as {name: reason}.

    For a startup check that wants to list every problem at once rather than abort on
    the first.

    `names=None` (the default) audits every registered endpoint. An EMPTY iterable
    means exactly what it says: no endpoints, so nothing is missing. The distinction
    matters because `resolve_all` forwards its own argument here -- with `or` deciding
    this, `resolve_all([])` audited all nine and raised "9 of 0 endpoints are not
    configured", so a caller asking for nothing got a hard failure. Falsy is not the
    same as absent; test for None.
    """
    wanted = sorted(ENDPOINTS) if names is None else list(names)
    out: Dict[str, str] = {}
    for n in wanted:
        try:
            resolve(n, env=env)
        except ConfigError as exc:
            out[n] = str(exc)
    return out


def resolve_all(names: Iterable[str],
                env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Resolve several endpoints, raising once with ALL the failures.

    Aborting on the first missing variable makes configuring a fresh deployment an
    N-round guessing game. This reports the whole set.
    """
    wanted = list(names)
    if not wanted:
        return {}
    bad = missing(wanted, env=env)
    if bad:
        body = "\n\n".join(bad[n] for n in wanted if n in bad)
        raise ConfigError(
            "%d of %d endpoints are not configured:\n\n%s"
            % (len(bad), len(wanted), body))
    return {n: resolve(n, env=env) for n in wanted}


# Service names an in-network (Compose) deployment resolves instead of loopback. Kept
# beside the registry so the two example files are generated from one source and cannot
# drift apart; a compose file that renames a service must change this map.
COMPOSE_SERVICE = {
    "router": "router",
    "router_chat": "router",
    "router_health": "router",
    "ollama": "ollama",
    "embed": "embed",
    "kb": "kb",
    "arbiter": "arbiter",
    "broadcaster": "broadcaster",
    "search": "search",
    "portal": "portal",
}


def _in_network(spec: Endpoint, service: str) -> str:
    """Rewrite a loopback example to address `service` on the compose network.

    The port is taken from `spec.listens_on`, NOT from the example's own port. Carrying
    the example's port over is what put four wrong ports in
    `deploy/profiles/compose.env.example`: the examples came from a deployment that
    PUBLISHED these services on other ports, and on a container network nothing
    publishes anything -- you address the port the process bound.
    """
    if spec.listens_on is None:
        # A registry defect, not a deployment one, so it fails at import of this
        # function rather than producing a URL with no port in it.
        raise ValueError(
            "%s has no listens_on, so its in-network address cannot be derived"
            % spec.env)
    parts = urlsplit(spec.example)
    return "%s://%s:%d%s" % (parts.scheme, service, spec.listens_on, parts.path)


def describe(host: str = "loopback") -> str:
    """The registry as env-file content, so the docs cannot drift from the code.

    host="loopback" -- for running the router directly on a machine.
    host="compose"  -- for running it inside a container network.

    The two are SEPARATE ARTIFACTS on purpose. Inside a container, 127.0.0.1 is the
    container itself, so copying the loopback file into a Compose deployment points
    every endpoint at the container's own loopback and yields connection-refused --
    which is precisely the "fails several layers below the real mistake" symptom this
    module was written to remove. Shipping one file for both would reintroduce it.
    """
    if host not in ("loopback", "compose"):
        raise ValueError("host must be 'loopback' or 'compose', not %r" % host)

    if host == "loopback":
        head = [
            "# RAMEAU endpoints -- values for running the router DIRECTLY ON A HOST.",
            "#",
            "# Generated from shared/resolver.py: `python shared/resolver.py",
            "# --env-example`. Edit the registry, not this file.",
            "#",
            "# NOT FOR A CONTAINER. Inside a container 127.0.0.1 is the container",
            "# itself, so these values would point every endpoint at the container's",
            "# own loopback. For Compose use deploy/profiles/compose.env.example,",
            "# which addresses services by name.",
            "#",
            "# There are no built-in defaults: an unset endpoint fails at resolution",
            "# naming the variable. BLANK EVERY ENDPOINT YOU DO NOT RUN: a blank",
            "# value counts as unset, which is the safe state, and a wrong endpoint",
            "# is worse than no endpoint -- the same reason RAMEAU_OLLAMA_URL ships",
            "# with no default at all.",
            "",
        ]
    else:
        head = [
            "# RAMEAU endpoints -- values for running INSIDE A CONTAINER NETWORK.",
            "#",
            "# Generated from shared/resolver.py: `python shared/resolver.py",
            "# --compose-example`. Edit the registry, not this file.",
            "#",
            "# Each host below is a COMPOSE SERVICE NAME, resolved by the container",
            "# network's DNS rather than by the host, which is why a loopback",
            "# address is wrong here.",
            "#",
            "# BLANK EVERY ENDPOINT YOU DO NOT RUN. Required, not optional, and the",
            "# one edit you cannot skip. As generated, SIX of these names are not",
            "# services in deploy/compose.yaml, which defines only router and",
            "# portal: the rest name capabilities v0.1 ships no image for. So this",
            "# file copied unedited points those endpoints at names the container",
            "# network cannot resolve. A blank value counts as unset, which is the",
            "# safe state; a wrong endpoint is worse than no endpoint.",
            "",
        ]
    lines = list(head)
    for name in sorted(ENDPOINTS):
        spec = ENDPOINTS[name]
        value = (spec.example if host == "loopback"
                 else _in_network(spec, COMPOSE_SERVICE[name]))
        lines.append("# %s -- %s" % (name, spec.purpose))
        if spec.required_for != "core":
            lines.append("#   only needed for: %s" % spec.required_for)
        lines.append("%s=%s" % (spec.env, value))
        lines.append("")
    return "\n".join(lines)


GENERATED = (
    (".env.example", "loopback"),
    ("deploy/profiles/compose.env.example", "compose"),
)


def write_generated(root: str) -> Dict[str, bool]:
    """Write both example files under `root`, as UTF-8 with LF and no BOM.

    The generator owns the write because a redirect does not, and the reason is Python
    rather than the shell: on Windows, text-mode stdout translates "\n" to "\r\n", so
    `python shared/resolver.py --env-example > .env.example` writes CRLF whatever shell
    runs it -- measured under Git Bash, not inferred. The file then differs from this
    module's output on every line while looking identical on screen, and a test
    comparing the two fails for a reason that has nothing to do with the registry.
    `--write-examples` writes bytes, so it produces LF on every platform.

    Returns {path: changed}.
    """
    out: Dict[str, bool] = {}
    for rel, mode in GENERATED:
        path = os.path.join(root, *rel.split("/"))
        body = describe(mode).encode("utf-8")
        try:
            with open(path, "rb") as fh:
                before = fh.read()
        except OSError:
            before = None
        with open(path, "wb") as fh:
            fh.write(body)
        out[rel] = before != body
    return out


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--env-example":
        sys.stdout.write(describe("loopback"))
    elif len(sys.argv) > 1 and sys.argv[1] == "--compose-example":
        sys.stdout.write(describe("compose"))
    elif len(sys.argv) > 1 and sys.argv[1] == "--write-examples":
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for _rel, _changed in sorted(write_generated(_root).items()):
            print("%-40s %s" % (_rel, "rewritten" if _changed else "unchanged"))
    else:
        bad = missing()
        for n in sorted(ENDPOINTS):
            if n in bad:
                print("UNSET    %-12s %s" % (n, ENDPOINTS[n].env))
            else:
                print("ok       %-12s %s" % (n, resolve(n)))
        sys.exit(1 if bad else 0)
