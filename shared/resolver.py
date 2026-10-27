"""Endpoint resolution for RAMEAU.

Every service address this router talks to is resolved here, from the environment, and
nowhere else. The module exists because of a specific failure mode found in the source
archive and recorded in RELEASE-SCOPE.md: the private deployment's addresses had been
redacted *in place*, leaving code like

    os.getenv("SOME_URL", "http://192.168.x.4:8089")

Fourteen such call sites were measured across eight files. None of them leaks an address
any more -- but none of them works either. A user who does not set the variable gets a
DNS failure against a hostname that cannot exist, several layers below the thing they
actually got wrong, which is that they did not configure an endpoint.

So the rule here is: **no fallback to a literal address, ever.** An unset endpoint raises
at the point of resolution with the variable's name and an example value in the message.
Failing immediately and legibly is worth more than starting and misbehaving.

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
    """

    env: str
    purpose: str
    example: str
    aliases: tuple = ()
    required_for: str = "core"


# The registry below was built by scanning the source archive's own call sites rather
# than from memory, so the alias names are the ones the code actually used. Where an
# old name embedded a private machine name (HEARTH_*) or a GPU model (VEGA8), the
# canonical name is neutral and the old one is kept only as an alias.
ENDPOINTS: Dict[str, Endpoint] = {
    "router": Endpoint(
        env="RAMEAU_ROUTER_URL",
        purpose="the router's own OpenAI-compatible base; clients and self-checks use it",
        example="http://127.0.0.1:8089",
        aliases=("BIFROST_ROUTER_BASE", "BIFROST_A2A_PUBLIC_URL"),
    ),
    "router_chat": Endpoint(
        env="RAMEAU_ROUTER_CHAT_URL",
        purpose="full chat-completions URL, when a client needs the path and not the base",
        example="http://127.0.0.1:8089/v1/chat/completions",
        aliases=("BIFROST_ROUTER_URL",),
    ),
    "router_health": Endpoint(
        env="RAMEAU_ROUTER_HEALTH_URL",
        purpose="readiness/status probe target for deployment checks",
        example="http://127.0.0.1:3110/router/status",
        aliases=("BIFROST_ROUTER_HEALTH_URL",),
    ),
    "ollama": Endpoint(
        env="RAMEAU_OLLAMA_URL",
        purpose="the local inference backend serving the generation tiers",
        example="http://127.0.0.1:11434",
        aliases=("BIFROST_OLLAMA_URL",),
    ),
    "embed": Endpoint(
        env="RAMEAU_EMBED_URL",
        purpose="the embedding endpoint; kept separate so embeddings never contend "
                "with generation on one accelerator",
        example="http://127.0.0.1:11436",
        # HEARTH_VEGA8_URL named a specific machine and a specific iGPU. Neither
        # belongs in a public variable name, so it survives only as an alias.
        aliases=("HEARTH_VEGA8_URL",),
        required_for="embeddings",
    ),
    "kb": Endpoint(
        env="RAMEAU_KB_URL",
        purpose="the retrieval service backing knowledge-base lookups",
        example="http://127.0.0.1:8091",
        aliases=("BIFROST_KB_URL",),
        required_for="retrieval",
    ),
    "arbiter": Endpoint(
        env="RAMEAU_ARBITER_URL",
        purpose="the adjudication service for multi-candidate tier output",
        example="http://127.0.0.1:8082",
        aliases=("ARBITER_URL",),
        required_for="arbitration",
    ),
    "broadcaster": Endpoint(
        env="RAMEAU_BROADCASTER_URL",
        purpose="fan-out of request/response events to observers",
        example="http://127.0.0.1:8092",
        aliases=("BROADCASTER_URL",),
        required_for="observability",
    ),
    "search": Endpoint(
        env="RAMEAU_SEARCH_URL",
        purpose="the self-hosted metasearch endpoint used by the research path",
        example="http://127.0.0.1:8888",
        aliases=("SEARXNG_URL",),
        required_for="search",
    ),
}


def _looks_like_a_url(value: str) -> Optional[str]:
    """Return a complaint about `value`, or None when it is usable.

    Deliberately shallow: this catches the mistakes that actually happen -- a bare
    host, a leftover redaction, a trailing slash -- and does not try to validate
    hostnames, which is the resolver's business to pass through, not to police.
    """
    if not value.strip():
        return "it is empty or whitespace"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return "it has no http:// or https:// scheme"
    if not parts.netloc:
        return "it has no host"
    host = parts.hostname or ""
    # A redacted octet is the exact failure this module was written for. The archive's
    # convention was a single "x" (192.168.x.4), but keying on single letters alone
    # would miss 192.168.xx.4. The general shape is a four-field host that MIXES
    # numeric and alphabetic fields -- which no real hostname and no real address does.
    # A wholly alphabetic four-field name (a.b.c.d) is a legitimate FQDN and passes.
    fields = host.split(".")
    if len(fields) == 4:
        numeric = [f for f in fields if f.isdigit()]
        alpha = [f for f in fields if f.isalpha()]
        if numeric and alpha and len(numeric) + len(alpha) == 4:
            return ("it still carries a redacted address (%r) -- this is a placeholder "
                    "from the source archive, not a working value" % host)
    return None


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
        if raw is None:
            continue
        complaint = _looks_like_a_url(raw)
        if complaint is not None:
            raise ConfigError(
                "%s is set but unusable: %s.\n"
                "  needed for: %s\n"
                "  example:    %s=%s"
                % (var, complaint, spec.purpose, spec.env, spec.example))
        if var != spec.env:
            # Honoured, but say so once: the alias names a private machine or a
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


def missing(names: Iterable[str] = (),
            env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Report which of `names` cannot be resolved, as {name: reason}.

    For a startup check that wants to list every problem at once rather than abort on
    the first. An empty `names` means every registered endpoint.
    """
    wanted = list(names) or sorted(ENDPOINTS)
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
    bad = missing(wanted, env=env)
    if bad:
        body = "\n\n".join(bad[n] for n in wanted if n in bad)
        raise ConfigError(
            "%d of %d endpoints are not configured:\n\n%s"
            % (len(bad), len(wanted), body))
    return {n: resolve(n, env=env) for n in wanted}


def describe() -> str:
    """The registry as .env.example content, so the docs cannot drift from the code."""
    lines = [
        "# RAMEAU endpoints. Every address the router uses is read from here.",
        "# There are no built-in defaults: an unset endpoint fails at startup with",
        "# the variable's name, rather than silently pointing somewhere unreachable.",
        "",
    ]
    for name in sorted(ENDPOINTS):
        spec = ENDPOINTS[name]
        lines.append("# %s -- %s" % (name, spec.purpose))
        if spec.required_for != "core":
            lines.append("#   only needed for: %s" % spec.required_for)
        lines.append("%s=%s" % (spec.env, spec.example))
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator convenience
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--env-example":
        sys.stdout.write(describe())
    else:
        bad = missing()
        for n in sorted(ENDPOINTS):
            if n in bad:
                print("UNSET    %-12s %s" % (n, ENDPOINTS[n].env))
            else:
                print("ok       %-12s %s" % (n, resolve(n)))
        sys.exit(1 if bad else 0)
