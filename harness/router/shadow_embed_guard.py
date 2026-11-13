"""
Admission control for the fire-and-forget shadow embed paths (2026-08-24).

WHY THIS EXISTS
---------------
Two advisory paths in main.py embed EVERY routed prompt against the Hearth
embed backend, both via `asyncio.create_task` (fire-and-forget, so neither
adds serving latency and neither reports failure to anyone):

    _shadow_record  -> _embed_for_cache -> POST 127.0.0.1:11436/api/embeddings
    _embed_shadow   -> embed_suggest    -> POST 127.0.0.1:8080/v1/embeddings
                                           (router-mediated; same :11436 backend)

Both land on the SAME Ollama instance -- the Vega 8 iGPU node on :11436, which
also serves the 1a-overflow chat tier. `fleet_config.json` co-locates
`1a-overflow` and `embed-hearth` there deliberately, but that means the chat
model and nomic-embed-text contend for one instance's model slots.
gen_capability_profiles.py:66 already records the consequence in the field:

    "The Vega8 embed backend (127.0.0.1:11436) unloads aggressively; the first
     call after idle cold-loads and the router 503s at its 3s embed timeout."

That is a thrash loop with no brake. Every failure is swallowed, so the next
request immediately schedules another embed against a backend that is still
cold-loading -- which on an iGPU/CPU path means repeated model load + prompt
eval, and `ollama_llama_server.exe` pinned at high CPU indefinitely. Nothing in
the current code limits concurrency either: N in-flight requests schedule N
concurrent embeds onto a backend that serves them serially.

WHAT THIS DOES
--------------
Bounded admission in front of both paths. Three independent brakes:

  1. In-flight cap  -- DROP (never queue) when the cap is reached. Queueing is
     what turns a slow backend into an unbounded task pile; these calls are
     advisory, so a dropped one costs nothing but a sample.
  2. Circuit breaker -- after N consecutive failures the breaker OPENS and the
     backend is left completely alone for a cooldown, then ONE half-open probe
     decides whether to close. This is the piece that ends the thrash: a
     cold-loading backend gets silence instead of more load.
  3. Sample rate    -- optional 0.0..1.0 throttle to keep the calibration
     signal at a fraction of the load.

A call that SUCCEEDS but takes longer than the slow threshold counts as a
failure for breaker purposes. On this backend a multi-second embed is the
cold-load signature, not a healthy call, and waiting for consecutive hard
failures would miss it entirely.

DEFAULTS ARE DELIBERATELY NON-INVASIVE: enabled, sample 1.0. In the healthy
case (embed warm, low concurrency) nothing here changes behavior -- the
brakes only engage under exactly the pathology described above.

OPERATIONAL NOTE
----------------
Every knob is an env var, and the router Deployment reads env via
`envFrom: configMapRef: bifrost-router-config`. So these are tunable by editing
k8s/bifrost-router-config.yaml and restarting -- NO image rebuild. That matters
here: per CLAUDE.md, fleet_config.json and capability_registry.json are BAKED
into the image, so a config-shaped fix to those would need a full rebuild and
`set image`. This one does not.

    BIFROST_SHADOW_EMBED_ENABLED=0   <- instant kill switch, both paths off
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("bifrost.shadow_embed_guard")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("bad float in %s=%r; using default %s", name, os.getenv(name), default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("bad int in %s=%r; using default %s", name, os.getenv(name), default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# Read once at import, like the rest of the router's env handling. Changing a
# knob means a pod restart -- which is what applying a ConfigMap edit does anyway.
ENABLED         = _env_bool("BIFROST_SHADOW_EMBED_ENABLED", True)
SAMPLE_RATE     = _env_float("BIFROST_SHADOW_EMBED_SAMPLE", 1.0)
MAX_INFLIGHT    = _env_int("BIFROST_SHADOW_EMBED_MAX_INFLIGHT", 2)
SLOW_MS         = _env_float("BIFROST_SHADOW_EMBED_SLOW_MS", 5000.0)
FAIL_THRESHOLD  = _env_int("BIFROST_SHADOW_EMBED_FAIL_THRESHOLD", 3)
COOLDOWN_S      = _env_float("BIFROST_SHADOW_EMBED_COOLDOWN_S", 60.0)
MAX_COOLDOWN_S  = _env_float("BIFROST_SHADOW_EMBED_MAX_COOLDOWN_S", 600.0)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

# Drop reasons, in the order try_admit checks them. Kept as constants so the
# counters and the log lines can never drift apart.
_R_DISABLED  = "disabled"
_R_BREAKER   = "breaker_open"
_R_SAMPLED   = "sampled_out"
_R_INFLIGHT  = "inflight_cap"


@dataclass
class _Stats:
    admitted: int = 0
    ok: int = 0
    failed: int = 0
    slow: int = 0
    dropped: dict = field(default_factory=lambda: {
        _R_DISABLED: 0, _R_BREAKER: 0, _R_SAMPLED: 0, _R_INFLIGHT: 0,
    })
    breaker_opens: int = 0


class ShadowEmbedGuard:
    """Per-path admission control. Thread-safe; cheap enough for the hot path.

    A plain `threading.Lock` rather than an asyncio primitive on purpose: every
    section it guards is a handful of integer updates with no await inside, and
    a sync lock keeps `try_admit` callable from any context (tests included)
    without needing a running loop.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._inflight = 0
        self._consecutive_failures = 0
        self._state = CLOSED
        self._open_until = 0.0
        self._cooldown = COOLDOWN_S
        self.stats = _Stats()

    # -- admission ---------------------------------------------------------
    def try_admit(self, *, now: float | None = None) -> tuple[bool, str]:
        """(admitted, reason). Never blocks, never queues, never raises."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if not ENABLED:
                self.stats.dropped[_R_DISABLED] += 1
                return False, _R_DISABLED

            # Breaker first: an open breaker must cost the backend NOTHING,
            # so it short-circuits ahead of sampling and the in-flight cap.
            if self._state == OPEN:
                if now < self._open_until:
                    self.stats.dropped[_R_BREAKER] += 1
                    return False, _R_BREAKER
                # Cooldown elapsed -- promote to half-open and let exactly one
                # probe through. _inflight gates any second caller out.
                self._state = HALF_OPEN
                logger.info("shadow embed guard [%s]: breaker half-open, probing", self.name)

            if self._state == HALF_OPEN and self._inflight >= 1:
                self.stats.dropped[_R_BREAKER] += 1
                return False, _R_BREAKER

            if self._state == CLOSED and SAMPLE_RATE < 1.0:
                if SAMPLE_RATE <= 0.0 or random.random() >= SAMPLE_RATE:
                    self.stats.dropped[_R_SAMPLED] += 1
                    return False, _R_SAMPLED

            if self._state == CLOSED and self._inflight >= MAX_INFLIGHT:
                self.stats.dropped[_R_INFLIGHT] += 1
                return False, _R_INFLIGHT

            self._inflight += 1
            self.stats.admitted += 1
            return True, "admitted"

    # -- completion --------------------------------------------------------
    def release(self, *, ok: bool, latency_ms: float, detail: str = "") -> None:
        """Record an admitted call's outcome. Slow-but-successful counts as failure."""
        now = time.monotonic()
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

            slow = ok and latency_ms > SLOW_MS
            if slow:
                self.stats.slow += 1
            healthy = ok and not slow

            if healthy:
                self.stats.ok += 1
                self._consecutive_failures = 0
                if self._state in (HALF_OPEN, OPEN):
                    logger.info(
                        "shadow embed guard [%s]: breaker CLOSED after healthy probe (%.0fms)",
                        self.name, latency_ms,
                    )
                self._state = CLOSED
                self._cooldown = COOLDOWN_S  # reset the backoff ladder
                return

            self.stats.failed += 1
            self._consecutive_failures += 1

            # A failed half-open probe re-opens immediately and backs off, so a
            # backend that stays sick is retried less and less often rather than
            # once per cooldown forever.
            if self._state == HALF_OPEN:
                self._cooldown = min(self._cooldown * 2.0, MAX_COOLDOWN_S)
                self._trip(now, latency_ms, slow, detail)
                return

            if self._consecutive_failures >= FAIL_THRESHOLD:
                self._trip(now, latency_ms, slow, detail)

    def _trip(self, now: float, latency_ms: float, slow: bool, detail: str) -> None:
        """Caller must hold self._lock."""
        was_open = self._state == OPEN
        self._state = OPEN
        self._open_until = now + self._cooldown
        if not was_open:
            self.stats.breaker_opens += 1
        logger.warning(
            "shadow embed guard [%s]: breaker OPEN for %.0fs after %d consecutive "
            "failures (last: %s%s%s). Advisory embeds suspended -- routing is "
            "UNAFFECTED. Set BIFROST_SHADOW_EMBED_ENABLED=0 to disable permanently.",
            self.name, self._cooldown, self._consecutive_failures,
            "slow " if slow else "", f"{latency_ms:.0f}ms", f" {detail}" if detail else "",
        )

    # -- introspection -----------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            remaining = max(0.0, self._open_until - time.monotonic())
            return {
                "name": self.name,
                "enabled": ENABLED,
                "state": self._state,
                "inflight": self._inflight,
                "consecutive_failures": self._consecutive_failures,
                "cooldown_s": self._cooldown,
                "reopens_in_s": round(remaining, 1) if self._state == OPEN else 0.0,
                "admitted": self.stats.admitted,
                "ok": self.stats.ok,
                "failed": self.stats.failed,
                "slow": self.stats.slow,
                "breaker_opens": self.stats.breaker_opens,
                "dropped": dict(self.stats.dropped),
            }

    def reset(self) -> None:
        """Test hook. Not called by serving code."""
        with self._lock:
            self._inflight = 0
            self._consecutive_failures = 0
            self._state = CLOSED
            self._open_until = 0.0
            self._cooldown = COOLDOWN_S
            self.stats = _Stats()


_GUARDS: dict[str, ShadowEmbedGuard] = {}
_GUARDS_LOCK = threading.Lock()


def guard(name: str) -> ShadowEmbedGuard:
    with _GUARDS_LOCK:
        g = _GUARDS.get(name)
        if g is None:
            g = ShadowEmbedGuard(name)
            _GUARDS[name] = g
        return g


class _Slot:
    """Handle yielded by `admit`. `admitted` is False when the call was dropped."""

    __slots__ = ("admitted", "_ok", "_detail")

    def __init__(self, admitted: bool) -> None:
        self.admitted = admitted
        self._ok = True
        self._detail = ""

    def failed(self, detail: str = "") -> None:
        """Mark this attempt unhealthy.

        Needed because both shadow paths fail-open: `embed_suggest` returns None
        and `_shadow_record` swallows its own exception, so the breaker cannot
        learn about a failure from a propagating exception alone.
        """
        self._ok = False
        self._detail = detail


@asynccontextmanager
async def admit(name: str):
    """
        async with admit("routing_cache") as slot:
            if not slot.admitted:
                return
            ...advisory work...
    """
    g = guard(name)
    ok, _reason = g.try_admit()
    slot = _Slot(ok)
    if not ok:
        yield slot
        return
    t0 = time.monotonic()
    try:
        yield slot
    except BaseException:
        g.release(ok=False, latency_ms=(time.monotonic() - t0) * 1000.0, detail="exception")
        raise
    else:
        g.release(
            ok=slot._ok,
            latency_ms=(time.monotonic() - t0) * 1000.0,
            detail=slot._detail,
        )


def snapshot_all() -> dict:
    with _GUARDS_LOCK:
        guards = list(_GUARDS.values())
    return {
        "config": {
            "enabled": ENABLED,
            "sample_rate": SAMPLE_RATE,
            "max_inflight": MAX_INFLIGHT,
            "slow_ms": SLOW_MS,
            "fail_threshold": FAIL_THRESHOLD,
            "cooldown_s": COOLDOWN_S,
            "max_cooldown_s": MAX_COOLDOWN_S,
        },
        "paths": {g.name: g.snapshot() for g in guards},
    }
