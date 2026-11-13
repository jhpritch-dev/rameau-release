"""routing_cache.py -- Shadow routing cache for BIFROST Phase-2 (LOCKED 2026-05-21).

Per BIFROST-ROUTER-MAP-classifier-cache-2026-05-21.md sections 5, 6, 8, 9.

Implements:
  - In-process brute-force cosine over a NumPy array (no FAISS / sqlite-vec)
  - Vector-only persistence to {BIFROST_TELEMETRY_DIR}/routing_cache.bin (no prompt text)
  - Lock-guarded writes; reads against an immutable snapshot
  - Periodic flush + load-on-startup
  - config_version stamping + invalidation (map Section 5)
  - Age-out eviction at max_size (FIFO by timestamp; map Open Decision 1)

Phase A (ROUTER-CACHE-SHADOW-BUILD): serves NOTHING. Reads happen but the result is
LOGGED by callers (not consumed for routing). Writes happen from the AUTHORITATIVE
classify result. The cache is the future Phase-B reservoir; Phase A fills it.

OD1 starting values (ratified 2026-05-22): max_size=2048, age-out by timestamp, no
hit_count tracking. Flagged for tuning.

TIGHT_START=0.85 ratified after Phase 0 cosine-spread check: cross-topic max=0.6162
(noise floor), paraphrase min=0.7088 (intent-equivalent ceiling). 0.85 is 0.22 above
the noise floor and catches structured paraphrases for measurable shadow-record volume.

2026-10-01: that 0.85 is now the LIBRARY DEFAULT only -- the effective value is
env-overridable (see BIFROST_ROUTING_CACHE_THRESHOLD below) and PRODUCTION RUNS 0.90.
The Phase 0 numbers above still explain the floor of the range: paraphrase similarity
starts at 0.7088, so raising the threshold trades paraphrase recall for precision, and
it was measured rather than guessed. On 5,104 live entries, 0.85 -> 0.90 declines 27 of
the served answers, preventing 11 WRONG routes at the cost of 16 right ones, and moves
the served error rate 5.21% -> 4.96%. It was taken because the 0.875-0.900 bucket alone
disagrees 53.3% of the time. Above 0.90 the trade inverts badly (0.95 loses 54 right to
stop 23 wrong), so DO NOT keep raising this: ~80% of the error sits at similarity >= 0.99,
where 113 byte-identical vectors carry conflicting bands and no threshold can reach it.
"""
import hashlib
import json
import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("bifrost.routing_cache")

DEFAULT_MAX_SIZE = 16384   # 2026-09-02: 2048 -> 16384. Safe only because lookup no
# longer rebuilds its matrix per call and flush no longer expands vectors into python
# floats; see RoutingCache._snapshot and flush(). Bounded by the pod's 1 GiB limit:
# resident matrix ~48 MB, entry vectors ~48 MB, flush blob ~48 MB.
DEFAULT_FLUSH_INTERVAL_S = 60.0
DEFAULT_PERSIST_FILENAME = "routing_cache.bin"

# 2026-10-01: OVERRIDABLE. Production runs BIFROST_ROUTING_CACHE_THRESHOLD=0.90; see the
# module docstring for the measurement that justifies 0.90 and rules out anything higher.
# The LIBRARY DEFAULT STAYS 0.85 on purpose: smoke_routing_cache.py imports this constant and
# asserts a HIT at a fixture similarity, so raising the default would break the suite rather
# than change production. Reverted by unsetting the var -- no rebuild.
# Logged at startup by RoutingCache.__init__ for the same reason the floor is: an env-defaulted
# behavior has been live and unnoticed in this router before.
_THRESHOLD_ENV = "BIFROST_ROUTING_CACHE_THRESHOLD"
_THRESHOLD_FALLBACK = 0.85
_threshold_raw = os.environ.get(_THRESHOLD_ENV)
try:
    TIGHT_START_THRESHOLD = (_THRESHOLD_FALLBACK if _threshold_raw is None
                             else float(_threshold_raw))
except ValueError:
    TIGHT_START_THRESHOLD = _THRESHOLD_FALLBACK
    log.warning(
        "routing_cache %s=%r is not a float; using %s",
        _THRESHOLD_ENV, _threshold_raw, _THRESHOLD_FALLBACK)
# 2026-09-30: OVERRIDABLE, because the floor is the only lever that can neutralize cache
# entries ALREADY ON DISK. S-1 (ff79708) stopped non-authoritative WRITES -- the write is
# now gated on _AUTHORITATIVE_ROUTING_SOURCES = {"npu"} -- but it does nothing about what
# was stored before it. Measured 2026-09-30 in the live routing_cache.bin: 1,178 of 6,346
# servable entries sit at confidence <= 0.85 against NPU's >= 0.90, all written 08-31 ..
# 09-30 17:28:48Z, i.e. before S-1 went live at 21:40:06Z. lookup() takes the SINGLE
# nearest neighbor, so whenever one of those is nearest it decides the route -- exactly
# the thing S-1 forbids for new writes. Raising the floor turns them into below_floor
# MISSES, which fall through to a real classification: it fails SAFE and deletes nothing.
#
# The LIBRARY DEFAULT STAYS 0.7 on purpose. smoke_routing_cache.py and smoke_canary_v120.py
# both construct RoutingCache() with no floor and write fixtures at 0.8/0.85 expecting
# HITS; moving the default would rewrite what those tests assert. Production sets
# BIFROST_ROUTING_CACHE_CONF_FLOOR=0.9 on the deployment instead, which is also how it is
# reverted -- unset the var, no rebuild.
#
# An env-defaulted behavior has bitten this router before (a live-data flag off by an env
# DEFAULT, unnoticed), so RoutingCache.__init__ LOGS the effective floor at startup and a
# malformed value warns rather than silently reverting.
_CONF_FLOOR_ENV = "BIFROST_ROUTING_CACHE_CONF_FLOOR"
_CONF_FLOOR_FALLBACK = 0.7
_conf_floor_raw = os.environ.get(_CONF_FLOOR_ENV)
try:
    DEFAULT_CONFIDENCE_FLOOR = (_CONF_FLOOR_FALLBACK if _conf_floor_raw is None
                                else float(_conf_floor_raw))
except ValueError:
    DEFAULT_CONFIDENCE_FLOOR = _CONF_FLOOR_FALLBACK
    log.warning(
        "routing_cache %s=%r is not a float; using %s",
        _CONF_FLOOR_ENV, _conf_floor_raw, _CONF_FLOOR_FALLBACK)


@dataclass
class CacheEntry:
    """One stored decision. Vector-only; no prompt text (privacy parity with telemetry.py)."""
    vector: np.ndarray         # shape (768,), float32 (nomic-embed-text)
    band: str                  # ComplexityBand value: TRIVIAL/MODERATE/COMPLEX/FRONTIER
    confidence: float
    config_version: str        # sha256(classifier_config + engine_id)[:16]
    timestamp: float           # epoch seconds


@dataclass
class CacheLookupResult:
    """What the lookup observed. In Phase A this is LOGGED to the shadow record,
    NOT used to route. miss_reason is one of:
      below_threshold | below_floor | config_mismatch | empty_cache | <None for hit>
    """
    hit: bool
    similarity: float
    cached_band: Optional[str] = None
    cached_confidence: Optional[float] = None
    cached_config_version: Optional[str] = None
    miss_reason: Optional[str] = None


class RoutingCache:
    """In-process routing cache. Reads use an immutable snapshot (no lock during scan);
    writes/eviction/flush are lock-guarded.

    NOT microseconds, and not free. The cosine scan is CPU-bound and SYNCHRONOUS inside an
    async handler, so its cost blocks every concurrent request, not just its own. Measured in
    the router pod: 0.11 ms at 2048 entries and 4.27 ms at 16384 -- but only because the
    matrix and its row norms are kept resident. Rebuilding them per call, as this class did
    before 2026-09-02, cost ~50 ms mean / 76 ms p95 at 2048 and dominated everything else.
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        persist_path: Optional[Path] = None,
        threshold: float = TIGHT_START_THRESHOLD,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    ):
        self._entries: list[CacheEntry] = []
        self._lock = threading.RLock()
        self._max_size = max_size
        self._flush_interval = flush_interval_s
        self._threshold = threshold
        self._confidence_floor = confidence_floor
        # Never let the effective floor be invisible: it is env-defaulted and it decides
        # whether a stored entry can route a request at all.
        log.info("routing_cache confidence_floor=%s (env %s=%r, default %s)",
                 self._confidence_floor, _CONF_FLOOR_ENV, _conf_floor_raw,
                 _CONF_FLOOR_FALLBACK)
        log.info("routing_cache threshold=%s (env %s=%r, default %s)",
                 self._threshold, _THRESHOLD_ENV, _threshold_raw, _THRESHOLD_FALLBACK)
        self._dirty = False              # needs flushing to DISK
        # Resident search matrix. Distinct from _dirty: that one tracks persistence, this one
        # tracks whether the matrix still corresponds to _entries.
        self._mat: Optional[np.ndarray] = None
        self._mat_norms: Optional[np.ndarray] = None
        self._mat_stale = True
        self._last_flush = time.time()
        if persist_path is None:
            tel_dir = os.environ.get("BIFROST_TELEMETRY_DIR", ".")
            persist_path = Path(tel_dir) / DEFAULT_PERSIST_FILENAME
        self._persist_path = Path(persist_path)

    def lookup(self, vector: np.ndarray, current_config_version: str) -> CacheLookupResult:
        """Cosine-similarity lookup against the snapshot.
        Phase A: caller LOGS the result; routing uses the authoritative classifier."""
        snapshot, mat, mat_norms = self._snapshot()
        if not snapshot:
            return CacheLookupResult(hit=False, similarity=0.0, miss_reason="empty_cache")
        # Brute-force cosine against the RESIDENT matrix (no lock held during the scan, and
        # no per-call rebuild -- that rebuild was 84-97 percent of this function's cost).
        v = np.asarray(vector, dtype=np.float32)
        v_norm = np.linalg.norm(v)
        denoms = mat_norms * v_norm
        denoms = np.where(denoms == 0.0, 1e-9, denoms)
        sims = (mat @ v) / denoms
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        best = snapshot[best_idx]
        # HIT criteria per map Section 2: sim >= threshold AND conf >= floor AND config_version matches
        if best_sim < self._threshold:
            return CacheLookupResult(
                hit=False, similarity=best_sim, miss_reason="below_threshold",
                cached_band=best.band, cached_confidence=best.confidence,
                cached_config_version=best.config_version,
            )
        if best.confidence < self._confidence_floor:
            return CacheLookupResult(
                hit=False, similarity=best_sim, miss_reason="below_floor",
                cached_band=best.band, cached_confidence=best.confidence,
                cached_config_version=best.config_version,
            )
        if best.config_version != current_config_version:
            return CacheLookupResult(
                hit=False, similarity=best_sim, miss_reason="config_mismatch",
                cached_band=best.band, cached_confidence=best.confidence,
                cached_config_version=best.config_version,
            )
        return CacheLookupResult(
            hit=True, similarity=best_sim,
            cached_band=best.band, cached_confidence=best.confidence,
            cached_config_version=best.config_version,
        )

    def _snapshot(self):
        """Return (entries, matrix, row_norms) that correspond to one another.

        Rebuilt only when the entry list has actually changed. Everything is produced under
        the lock so the three cannot disagree; the arrays are replaced wholesale rather than
        mutated, so holding references to them outside the lock stays safe while a concurrent
        write proceeds.
        """
        with self._lock:
            if not self._entries:
                return [], None, None
            if (self._mat_stale or self._mat is None
                    or self._mat.shape[0] != len(self._entries)):
                self._mat = np.stack([e.vector for e in self._entries])
                self._mat_norms = np.linalg.norm(self._mat, axis=1)
                self._mat_stale = False
            return list(self._entries), self._mat, self._mat_norms

    def write(self, vector: np.ndarray, band: str, confidence: float, config_version: str) -> None:
        """Append an authoritative-quality entry (map Section 4).
        Triggers periodic flush if interval elapsed."""
        entry = CacheEntry(
            vector=np.asarray(vector, dtype=np.float32),
            band=band,
            confidence=float(confidence),
            config_version=config_version,
            timestamp=time.time(),
        )
        with self._lock:
            self._entries.append(entry)
            # Age-out eviction (FIFO by timestamp) when over capacity
            if len(self._entries) > self._max_size:
                self._entries.sort(key=lambda e: e.timestamp)
                self._entries = self._entries[-self._max_size:]
            self._dirty = True
            self._mat_stale = True       # entries changed; the resident matrix no longer matches
        if time.time() - self._last_flush >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        """Persist current entries to disk. Atomic via tmp + rename. Vector-only
        (privacy parity)."""
        with self._lock:
            if not self._dirty:
                return
            # v2 format. The v1 payload called e.vector.tolist(), turning each 768-float32
            # vector into 768 PYTHON floats at ~32 bytes apiece -- roughly 400 MB transient at
            # 16384 entries, against a 1 GiB pod limit, on a 60-second timer. Pickling one
            # numpy array keeps the vectors as raw bytes (~48 MB) and is far faster.
            payload = {
                "format": 2,
                "vectors": (np.stack([e.vector for e in self._entries])
                            if self._entries else None),
                "meta": [
                    {
                        "band": e.band,
                        "confidence": e.confidence,
                        "config_version": e.config_version,
                        "timestamp": e.timestamp,
                    }
                    for e in self._entries
                ],
            }
            self._last_flush = time.time()
            self._dirty = False
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self._persist_path)
            log.info(f"routing_cache flush entries={len(payload['meta'])} "
                     f"path={self._persist_path}")
        except Exception as e:
            log.warning(f"routing_cache flush failed: {type(e).__name__}: {e}")

    def load_from_disk(self) -> int:
        """Load persisted entries on startup. Best-effort. Returns count loaded."""
        if not self._persist_path.exists():
            log.info(f"routing_cache no persist file at {self._persist_path}; starting empty")
            return 0
        try:
            with open(self._persist_path, "rb") as f:
                data = pickle.load(f)
            # BOTH formats are accepted. A live cache file written by v1 is already on disk and
            # is currently serving; refusing it would silently discard the working cache and
            # look like a cold start rather than an error.
            if isinstance(data, dict) and data.get("format") == 2:
                vecs = data.get("vectors")
                meta = data.get("meta") or []
                entries = [
                    CacheEntry(
                        vector=np.asarray(vecs[i], dtype=np.float32),
                        band=m["band"],
                        confidence=m["confidence"],
                        config_version=m["config_version"],
                        timestamp=m["timestamp"],
                    )
                    for i, m in enumerate(meta)
                ]
            else:
                entries = [
                    CacheEntry(
                        vector=np.asarray(d["vector"], dtype=np.float32),
                        band=d["band"],
                        confidence=d["confidence"],
                        config_version=d["config_version"],
                        timestamp=d["timestamp"],
                    )
                    for d in data
                ]
            # Honor the current cap: a file written under a larger max_size must not silently
            # reinstate it. Keep the NEWEST entries, matching write()'s FIFO age-out.
            if len(entries) > self._max_size:
                entries.sort(key=lambda e: e.timestamp)
                entries = entries[-self._max_size:]
            with self._lock:
                self._entries = entries
                self._last_flush = time.time()
                self._dirty = False
                self._mat_stale = True
            log.info(f"routing_cache loaded entries={len(entries)} from {self._persist_path}")
            return len(entries)
        except Exception as e:
            log.warning(f"routing_cache load failed: {type(e).__name__}: {e}; starting empty")
            return 0

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


def compute_config_version(classifier_cfg: Any, engine_id: str = "heuristic-v1") -> str:
    """Hash over (classifier_config + engine_id) per map Section 5.
    Bumping engine_id (e.g. heuristic-v1 -> npu-gemma3-4b) invalidates all prior entries.
    Editing classifier_config (keywords, thresholds, multi_file_threshold) also invalidates."""
    if hasattr(classifier_cfg, "model_dump"):
        blob = json.dumps(classifier_cfg.model_dump(), sort_keys=True)
    elif hasattr(classifier_cfg, "__dict__"):
        blob = json.dumps(classifier_cfg.__dict__, sort_keys=True, default=str)
    else:
        blob = str(classifier_cfg)
    full = blob + "|" + engine_id
    return hashlib.sha256(full.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Phase B: the SERVE path (2026-09-01).
#
# Phase A embedded every routed prompt, logged what the cache WOULD have
# answered, and discarded it -- paying the full cost for none of the benefit.
# This is the consuming half.
#
# OFF BY DEFAULT. The switch is an environment variable, not config: config.py
# is protected and fleet_config.json is baked into the image byte-exact, so an
# env var is the only mechanism that flips this without a rebuild and without
# touching a protected file. A deployed image is therefore inert until the
# operator deliberately enables it, which also gives a clean A/B on one image.
#
# BAND ALLOWLIST: all four levels. TRIVIAL, MODERATE, COMPLEX, FRONTIER.
#
# Ratified by John 2026-09-01 ("four levels not three"), overriding an earlier
# two-band proposal from this same work. Two measurements taken afterwards say
# the four-level call was the right one, and that the earlier restriction was
# protecting against harms that were partly imaginary.
#
# Measured over 39,199 shadow rows from inference_events.db, keyed on the band
# the cache SAYS -- the only key a serve-time gate can test, since the
# authoritative band is unknown at serve time:
#
#     says       accuracy   downgrade-rate   downgrades
#     TRIVIAL     98.76%        1.24%            351
#     COMPLEX     90.69%        1.73%             38
#     FRONTIER    68.55%        0.00%              0
#     MODERATE    72.67%       11.16%            116
#
# ACCURACY IS THE WRONG HEADLINE HERE; DIRECTION IS. A "downgrade" means the
# cache named a WEAKER band than the classifier would have, so the request is
# answered by a weaker model -- a quality failure. An "upgrade" means a stronger
# model than needed -- it costs more and never hurts the answer.
#
# FRONTIER's downgrade rate is 0.00% and always will be: it is the top rank, so
# a FRONTIER answer cannot name anything weaker. Its 68.55% "accuracy" is
# entirely over-provisioning. Excluding it on that number, as an earlier draft
# of this work did, would have been guarding against an impossible failure while
# forcing the most expensive requests to pay the full 3.4 s classifier every
# time.
#
# MODERATE carries the only real exposure: 11.16% downgrade rate, roughly nine
# times any other band. Raising its threshold does NOT fix it (72.96% accuracy
# and 10.39% downgrade even at 0.95), so a per-band threshold was considered and
# rejected as ineffective for the one band it would need to help. If MODERATE
# misroutes show up in practice, narrow the gate operationally with
# BIFROST_ROUTING_CACHE_SERVE_BANDS -- no rebuild required -- rather than
# rewriting policy here.
#
# Note TRIVIAL contributes the largest ABSOLUTE number of downgrades (351) at a
# low rate, because its base is 28,381. Rate and count rank the bands
# differently; both are recorded above so neither can stand in for the other.
#
# This function does NOT write. The Phase A shadow write stays exactly as it is,
# so the cache keeps learning and a rollback is a one-line revert of the serve
# rather than an unwind of the feature.
#
# The embed that produces `vector` is the caller's responsibility, and so is its
# timeout. Promotion moves that embed onto the synchronous request path, which
# is the real cost of this change; serve() only decides what to do with a vector
# that already exists.
# ---------------------------------------------------------------------------

SERVE_ENV_VAR = "BIFROST_ROUTING_CACHE_SERVE"
SERVE_BANDS_ENV_VAR = "BIFROST_ROUTING_CACHE_SERVE_BANDS"
DEFAULT_SERVE_BANDS = frozenset({"TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"})

# Explicit truthy set. bool("0") and bool("false") are both True, so parsing by
# truthiness would turn "off" into "on" -- the gate must fail closed.
_SERVE_TRUTHY = frozenset({"1", "true", "on", "yes"})


@dataclass
class ServeDecision:
    """The serve path's verdict.

    `reason` is always populated, on hits as well as misses, because a decision
    that cannot say why it went the way it did is not auditable:
      hit | disabled | hint_bypass | no_vector | band_not_served | lookup_error
      | below_threshold | below_floor | config_mismatch | empty_cache
    """
    serve: bool
    reason: str
    band: Optional[str] = None
    confidence: Optional[float] = None
    similarity: float = 0.0
    lookup: Optional[CacheLookupResult] = None


def serve_enabled(env: Optional[Any] = None) -> bool:
    """True only when the operator explicitly enabled the serve path."""
    src = os.environ if env is None else env
    raw = src.get(SERVE_ENV_VAR)
    if raw is None:
        return False
    return str(raw).strip().lower() in _SERVE_TRUTHY


def serve_bands(env: Optional[Any] = None) -> frozenset:
    """Bands the cache is permitted to answer for.

    Overridable so the allowlist can be widened or narrowed without a rebuild.
    An unset or unparseable value falls back to the measured default rather
    than to "everything" -- a malformed override must not silently widen the
    gate. Unknown band names are dropped; if nothing survives, the default wins.
    """
    src = os.environ if env is None else env
    raw = src.get(SERVE_BANDS_ENV_VAR)
    if raw is None:
        return DEFAULT_SERVE_BANDS
    known = {"TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"}
    parsed = {p.strip().upper() for p in str(raw).split(",") if p.strip()}
    kept = parsed & known
    if not kept:
        log.warning("routing_cache: %s=%r yielded no known bands; using default %s",
                    SERVE_BANDS_ENV_VAR, raw, sorted(DEFAULT_SERVE_BANDS))
        return DEFAULT_SERVE_BANDS
    dropped = parsed - known
    if dropped:
        log.warning("routing_cache: %s ignoring unknown bands %s",
                    SERVE_BANDS_ENV_VAR, sorted(dropped))
    return frozenset(kept)


def serve(cache: Any,
          vector: Any,
          current_config_version: str,
          hint_present: bool = False,
          env: Optional[Any] = None) -> ServeDecision:
    """Decide whether the cache may answer this request instead of the classifier.

    Fails closed on every uncertain path: disabled, no vector, an out-of-allowlist
    band, a lookup miss, or ANY exception all return serve=False, and the caller
    then classifies exactly as it does today.

    `hint_present` carries the Phase A hint rule forward verbatim: a request
    carrying X-Complexity-Hint bypasses the cache entirely. The caller passes
    this because classification.hint_override is unreliable when the NPU is
    confident, so the raw presence of the header is what must be tested.
    """
    if not serve_enabled(env):
        return ServeDecision(serve=False, reason="disabled")
    if hint_present:
        return ServeDecision(serve=False, reason="hint_bypass")
    if vector is None:
        return ServeDecision(serve=False, reason="no_vector")

    try:
        result = cache.lookup(vector, current_config_version)
    except Exception as exc:                                          # noqa: BLE001
        # A cache fault must never fail a request. Classify instead.
        log.warning("routing_cache: serve lookup failed (%r); falling through", exc)
        return ServeDecision(serve=False, reason="lookup_error")

    if not result.hit:
        return ServeDecision(serve=False,
                             reason=result.miss_reason or "miss",
                             band=result.cached_band,
                             confidence=result.cached_confidence,
                             similarity=result.similarity,
                             lookup=result)

    if result.cached_band not in serve_bands(env):
        return ServeDecision(serve=False, reason="band_not_served",
                             band=result.cached_band,
                             confidence=result.cached_confidence,
                             similarity=result.similarity,
                             lookup=result)

    return ServeDecision(serve=True, reason="hit",
                         band=result.cached_band,
                         confidence=result.cached_confidence,
                         similarity=result.similarity,
                         lookup=result)
