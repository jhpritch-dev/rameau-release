"""Release-side wiring for the routing cache.

WHY THIS FILE EXISTS. `routing_cache.py` and `routing_cache_serve_path.py` are byte-identical
copies of the published source under `harness/router/` -- a CI step asserts that, so neither
may be edited here to make the release fit. Everything the release has to decide for itself
therefore lives in this file: where the embedding endpoint is, how the cache is constructed,
and how a genuine classification gets written back. main.py gets three calls and no policy.

That is the same split the harness gives as its reason for `routing_cache_serve_path.py`
existing, applied one layer out.

-----------------------------------------------------------------------------------------
THE ENDPOINT, AND WHY THERE IS NO LITERAL FALLBACK
-----------------------------------------------------------------------------------------
The harness resolves this as:

    os.getenv("BIFROST_ROUTING_CACHE_EMBED_URL", resolve("embed") + "/api/embeddings")

which is right about the important half -- `resolve()` RAISES when nothing is configured, so
an unconfigured deployment gets a loud ConfigError rather than a guess at someone's LAN -- and
inconsistent about the other half. `shared/resolver.py` records
`BIFROST_ROUTING_CACHE_EMBED_URL` as an ALIAS for the embed endpoint, whose `example` is an
ORIGIN (`http://127.0.0.1:11436`); the getenv above reads that same variable as a COMPLETE
POST target including the path. Both readings are documented, in different files, for one
variable. An operator who follows the registry gets a POST to the origin and a 404; an
operator who follows the harness puts a path into a variable three other consumers read as an
origin.

So this file resolves through the registry -- one alias chain, maintained in one place -- and
then decides the path by looking at what came back, accepting BOTH shapes. Measured against
`shared/resolver.py` on 2026-11-13: resolve() strips a bare trailing slash, passes any other
path through untouched, walks the aliases in registry order, and raises ConfigError on unset.

NOTHING HERE MAY FAIL A REQUEST. The serve path is contractually fail-closed (see
`routing_cache_serve_path.try_serve`), and the write-back below runs detached, after the
response, so neither an unreachable embedder nor a corrupt cache file can turn into a 500.
"""
import asyncio
import logging
import os
from urllib.parse import urlparse, urlunparse

import httpx
import numpy as np

import routing_cache
import routing_cache_serve_path
from shared.resolver import ConfigError, resolve

log = logging.getLogger("rameau.router.routing_cache")

# Ollama's native embeddings route. The cache stores 768-dim nomic-embed-text vectors and a
# vector is only comparable with others produced by the SAME model, so this is not a knob:
# changing the model silently makes every stored entry a false neighbour. Invalidation is the
# config_version hash's job (`routing_cache.compute_config_version`), and it does not hash
# this name -- which is why it is a constant here rather than an environment variable.
EMBED_MODEL = "nomic-embed-text"
EMBED_PATH = "/api/embeddings"

# 30 s, matching the published source. This is the ceiling on a COLD embed, not the serve
# budget: `routing_cache_serve_path.DEFAULT_SERVE_TIMEOUT_S` (250 ms) is what bounds the
# request path, and this timeout only ever applies to the detached write-back.
EMBED_TIMEOUT_S = 30.0

# `Connection: close` is kept from the published source. It was added there for a measured
# keepalive stall between a containerised router and a host-side embedder reached through a
# port forward -- a shape this release's compose file reproduces exactly (the embedder is a
# host-boundary endpoint that compose deliberately does not set), so the condition travels
# with the deployment topology rather than with the private fleet.
EMBED_HEADERS = {"Connection": "close"}


def embed_url(env=None):
    """The full POST target for the embedding request. Raises ConfigError when unconfigured.

    Accepts both documented shapes for the endpoint variable (see the module header):
    an ORIGIN, to which the Ollama route is appended, or a COMPLETE target, used as given.
    A caller that supplied a path meant it; appending to it would produce
    `/api/embeddings/api/embeddings` and a 404 that looks like a misconfigured embedder.
    """
    src = os.environ if env is None else env
    base = resolve("embed", env=src)
    parts = urlparse(base)
    if parts.path in ("", "/"):
        return urlunparse(parts._replace(path=EMBED_PATH))
    return base


async def embed(prompt, env=None, client=None):
    """Embed one prompt. Returns a float32 numpy vector; raises on any failure.

    Raising rather than returning None is deliberate: every caller here is already inside a
    fail-closed wrapper that records the reason, and a None vector would reach
    `routing_cache.serve()` as reason="no_vector", which cannot distinguish an unreachable
    embedder from an empty prompt.
    """
    url = embed_url(env)
    payload = {"model": EMBED_MODEL, "prompt": prompt}
    if client is not None:
        resp = await client.post(url, json=payload, headers=EMBED_HEADERS)
        resp.raise_for_status()
        return np.asarray(resp.json()["embedding"], dtype=np.float32)
    async with httpx.AsyncClient(timeout=EMBED_TIMEOUT_S) as c:
        resp = await c.post(url, json=payload, headers=EMBED_HEADERS)
        resp.raise_for_status()
        return np.asarray(resp.json()["embedding"], dtype=np.float32)


def build_cache(env=None):
    """Construct and warm the cache, or return None when the feature is off.

    RETURNING None WHEN DISABLED IS THE POINT, not a convenience. `load_from_disk()` calls
    `pickle.load` on a path under BIFROST_TELEMETRY_DIR, and an operator who mounts a host
    directory there to persist the cache across restarts has given the router an unpickle
    surface. With the serve switch off there is no reason to read that file at all, so a
    disabled deployment presents no such surface and pays no startup cost -- which also keeps
    the negative control honest, exactly as `serve_or_classify` argues for its own
    short-circuit.

    deploy/README.md documents the mount consequence for operators who do turn it on.
    """
    if not routing_cache.serve_enabled(env):
        return None
    cache = routing_cache.RoutingCache()
    loaded = cache.load_from_disk()
    return cache, loaded


def config_version(classifier_cfg):
    """The cache's invalidation key for this router's classifier.

    engine_id is "heuristic-v1" because that is what this release's classifier IS -- a
    keyword and threshold scorer in `classifier.py`. The published source bumps this string
    when the deciding engine changes, and entries written under one engine must never be
    served under another, so naming the engine we actually run is what makes the hash mean
    anything.
    """
    return routing_cache.compute_config_version(classifier_cfg, engine_id="heuristic-v1")


def should_write_back(routing_td, hint) -> bool:
    """Whether this result may be stored. A FUNCTION, so the rule can be tested.

    Two refusals, for different reasons:

    A CACHE-SERVED RESULT, because storing it feeds the cache its own output as though it were
    authoritative; its errors would then compound with nothing left to correct them. Only a
    genuine classification is authoritative enough to store.

    A HINTED REQUEST, because `X-Complexity-Hint` is the caller overriding the classifier for
    this one request, and the serve path already refuses to ANSWER a hinted request
    (reason="hint_bypass"). Storing what the hint produced would let one caller's override
    leak into every later request whose prompt embeds near it -- the serve-side guard kept, and
    the write side left open, would reintroduce by the back door exactly what it excludes.

    Tested against the telemetry dict `routing_cache_serve_path.routing_td()` actually builds,
    not against a hand-written one, because the key spelling is the whole mechanism.
    """
    if hint:
        return False
    return routing_td.get("routing_source") != routing_cache_serve_path.ROUTING_SOURCE


# Fire-and-forget tasks need a strong reference or the event loop may collect them mid-flight.
_pending = set()


def schedule_write_back(prompt, classification, cache, cfg_version, env=None):
    """Embed and store a GENUINE classification, detached from the request.

    WHY DETACHED. The serve path embeds, and on a decline throws that vector away. Re-embedding
    synchronously to store it would charge every cache MISS the cost of the thing the cache
    exists to avoid. Running it after the response costs the request nothing, which is how the
    published source's shadow phase worked before it ever served.

    WHY ONLY A GENUINE CLASSIFICATION. A request answered FROM the cache must not be written
    BACK to it; that feeds the cache its own output as authoritative and lets its errors
    compound with nothing to correct them. The caller enforces this by passing only results
    whose routing_source is not `routing_cache_serve_path.ROUTING_SOURCE` -- and a test asserts
    it, because the rule is invisible at this function's boundary.

    WITHOUT THIS THE CACHE CAN NEVER HOLD ANYTHING. Shipping the serve path alone would give an
    operator a feature that reads as present, enables without error, and returns a miss for
    every request it ever sees.
    """
    if cache is None:
        return None

    async def _run():
        try:
            vector = await embed(prompt, env=env)
            cache.write(
                vector=vector,
                band=classification.band.value,
                confidence=float(classification.confidence),
                config_version=cfg_version,
            )
            cache.flush()
        except ConfigError as exc:
            # Unconfigured, not broken. One line, and never a traceback: with the switch on and
            # no embedder configured this would otherwise fire on every single request.
            log.warning("routing_cache write-back skipped: %s", exc)
        except Exception as exc:                                          # noqa: BLE001
            log.warning("routing_cache write-back failed (%s: %s)", type(exc).__name__, exc)

    task = asyncio.create_task(_run())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def status(cache):
    """Cache state for /status, so an operator can see what it is doing without the boot log.

    The startup line is written once; a reader during an incident needs the live answer. This
    reports whether the switch is on SEPARATELY from whether a cache object exists, because
    those come apart: the switch is read at startup, so turning it on in the environment of a
    running container changes `serve_enabled()` and nothing else.
    """
    enabled = routing_cache.serve_enabled()
    if cache is None:
        return {
            "serving": False,
            "serve_enabled": enabled,
            "entries": 0,
            "reason": "not constructed at startup (serve switch was off)",
        }
    return {
        "serving": enabled,
        "serve_enabled": enabled,
        "entries": cache.size(),
        "cap": routing_cache.DEFAULT_MAX_SIZE,
        "bands": sorted(routing_cache.serve_bands()),
    }
