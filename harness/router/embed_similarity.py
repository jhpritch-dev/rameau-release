"""
Embed second-signal for routing (npu-classifier-embed brief, 2026-06-25).

ADVISORY ONLY. The NPU classifier (FLM qwen3:1.7b @ :8003) is authoritative for
the routing band; the rule classifier is its fallback. This module supplements
*both* with a semantic-similarity signal from the always-on Hearth embed model.
It NEVER overrides the routing decision -- it returns a band suggestion plus a
confidence score for telemetry and discrepancy logging. Current policy (see the
caller in main.py): log when embed disagrees with the chosen band; keep the
authoritative band.

Fail-open: on ANY failure (embed down, timeout, missing/stale profiles, dim
mismatch, parse error) returns None so routing never blocks on the embed path.

Profiles: capability_profiles.json holds one centroid vector per ComplexityBand,
precomputed by gen_capability_profiles.py from representative example prompts.
Cosine similarity of the incoming prompt's embedding against each centroid gives
the suggested band. The profiles file is hot-reloaded on mtime change (same
pattern as fleet_config.json / capability_registry.json).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import ComplexityBand

logger = logging.getLogger("bifrost.embed_similarity")

EMBED_URL = os.getenv("BIFROST_EMBED_URL", "http://192.168.x.4:8089/v1/embeddings")
EMBED_MODEL = os.getenv("BIFROST_EMBED_MODEL", "bifrost-embed-hearth")
EMBED_TIMEOUT_S = float(os.getenv("BIFROST_EMBED_TIMEOUT_S", "2.0"))

_PROFILES_PATH = Path(__file__).parent / "capability_profiles.json"
_VALID_BANDS = {b.value for b in ComplexityBand}

# hot-reload cache
_PROFILES: dict[str, list[float]] | None = None
_PROFILES_MTIME: float | None = None


@dataclass
class EmbedSuggestion:
    band: ComplexityBand
    score: float                 # cosine sim to the winning centroid
    margin: float                # winner minus runner-up
    scores: dict[str, float]     # per-band cosine sims (for telemetry/debug)
    latency_ms: float


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _load_profiles() -> dict[str, list[float]] | None:
    """Load (and hot-reload) the per-band centroid vectors. None if unavailable."""
    global _PROFILES, _PROFILES_MTIME
    try:
        mt = _PROFILES_PATH.stat().st_mtime
    except OSError:
        return None
    if _PROFILES is None or mt != _PROFILES_MTIME:
        try:
            data = json.loads(_PROFILES_PATH.read_text(encoding="utf-8"))
            raw = data.get("profiles", data)
            profs = {
                band: [float(x) for x in vec]
                for band, vec in raw.items()
                if band in _VALID_BANDS and isinstance(vec, list) and vec
            }
            _PROFILES = profs or None
            _PROFILES_MTIME = mt
        except Exception as e:  # noqa: BLE001 -- advisory path, never raise
            logger.debug("embed profiles load failed: %s", e)
            return None
    return _PROFILES


async def embed_suggest(
    prompt: str,
    timeout: float = EMBED_TIMEOUT_S,
) -> EmbedSuggestion | None:
    """
    Return a semantic-similarity band suggestion for `prompt`, or None on any
    failure. Advisory only -- callers must treat None as 'no embed signal' and
    must NOT let this override the authoritative NPU/rule band.
    """
    if not prompt:
        return None
    profiles = _load_profiles()
    if not profiles:
        return None

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(EMBED_URL, json={"model": EMBED_MODEL, "input": prompt})
            r.raise_for_status()
            data = r.json()
        vec = [float(x) for x in data["data"][0]["embedding"]]
    except Exception as e:  # noqa: BLE001 -- fail-open
        logger.debug("embed_suggest call failed: %s", e)
        return None
    latency_ms = (time.time() - t0) * 1000.0

    scores = {band: _cosine(vec, centroid) for band, centroid in profiles.items()}
    scores = {b: s for b, s in scores.items() if s != 0.0}  # drop dim-mismatch/zero
    if not scores:
        return None

    best_band = max(scores, key=lambda b: scores[b])
    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
    try:
        band = ComplexityBand(best_band)
    except ValueError:
        return None
    return EmbedSuggestion(
        band=band,
        score=scores[best_band],
        margin=margin,
        scores=scores,
        latency_ms=latency_ms,
    )
