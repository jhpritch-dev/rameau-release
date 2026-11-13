# arbiter.py
# Dependencies: fastapi, uvicorn, httpx, pydantic
# Install: pip install fastapi uvicorn httpx pydantic
# Run: uvicorn arbiter:app --host 0.0.0.0 --port 8082

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.getenv("ARBITER_POLL_INTERVAL", "5"))
BROADCASTER_URL = os.getenv("BROADCASTER_URL", "http://192.168.x.4:8090")
DEBOUNCE_SECONDS = int(os.getenv("ARBITER_DEBOUNCE_SECONDS", "30"))
PROBE_TIMEOUT = int(os.getenv("ARBITER_PROBE_TIMEOUT", "5"))
MAX_TRANSITIONS = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arbiter")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OperatingMode(str, Enum):
    JARVIS = "JARVIS"
    WORKSHOP = "WORKSHOP"
    CLOUD_PLUS = "CLOUD_PLUS"
    WORKSTATION = "WORKSTATION"
    REMOTE = "REMOTE"
    NOMAD = "NOMAD"
    WORKSHOP_OFFLINE = "WORKSHOP_OFFLINE"
    DEGRADED = "DEGRADED"


class TierStatus(str, Enum):
    HEALTHY = "healthy"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    PLACEHOLDER = "placeholder"


# ---------------------------------------------------------------------------
# Tier availability per mode
# ---------------------------------------------------------------------------

MODE_TIER_POLICY: Dict[OperatingMode, Set[str]] = {
    OperatingMode.JARVIS: {
        "1a-coder", "1a-instruct", "1a-hearth", "1b", "2", "2.5",
        "3-Claude", "3-Gemini", "3-Groq",
        "E-base", "E-local",
    },
    OperatingMode.WORKSHOP: {
        "1a-coder", "1a-instruct", "1a-hearth",
        "3-Claude", "3-Gemini", "3-Groq",
        "E-base",
    },
    OperatingMode.CLOUD_PLUS: {
        "1a-hearth",
        "3-Claude", "3-Gemini", "3-Groq",
        "E-base",
    },
    OperatingMode.WORKSTATION: {
        "1a-hearth", "1b", "2", "2.5",
        "3-Claude", "3-Gemini", "3-Groq",
        "E-base", "E-local",
    },
    OperatingMode.REMOTE: {
        "1a-coder", "1a-instruct", "1a-hearth", "1b", "2", "2.5",
        "3-Claude", "3-Gemini", "3-Groq",
        "E-base", "E-local",
    },
    OperatingMode.NOMAD: {
        "1b", "2", "2.5",
        "3-Claude", "3-Gemini", "3-Groq",
    },
    OperatingMode.WORKSHOP_OFFLINE: {
        "1a-coder", "1a-instruct", "1a-hearth",
        "E-base",
    },
    OperatingMode.DEGRADED: {
        "3-Claude", "3-Gemini", "3-Groq",
    },
}

MODE_PROFILE_SUGGESTION: Dict[OperatingMode, str] = {
    OperatingMode.JARVIS: "B-Light",
    OperatingMode.WORKSHOP: "B-Dual",
    OperatingMode.CLOUD_PLUS: "B-Light",
    OperatingMode.WORKSTATION: "B-Light",
    OperatingMode.REMOTE: "B-Light",
    OperatingMode.NOMAD: "B-Light",
    OperatingMode.WORKSHOP_OFFLINE: "B-Heavy",
    OperatingMode.DEGRADED: "B-Light",
}


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class TierInfo(BaseModel):
    status: str
    detail: Optional[str] = None


class ConfirmedTransition(BaseModel):
    timestamp: float
    from_mode: OperatingMode
    to_mode: OperatingMode
    debounce_duration: float
    trigger: str
    suggested_profile: Optional[str] = None


class BroadcasterSnapshot(BaseModel):
    """Cached copy of last successful Broadcaster response."""
    mode: str = "DEGRADED"
    tiers: Any = []  # List of dicts from Broadcaster, converted to dict on ingestion
    signals: Dict[str, Any] = {}
    gpu_offload: bool = False
    gpu_detail: Optional[str] = None
    bifrost_profile: Optional[str] = None
    forge_profile: Optional[str] = None
    bifrost_loaded_models: Optional[List[str]] = None
    forge_loaded_models: Optional[List[str]] = None
    forge_vram_used_bytes: Optional[int] = None
    observer_connected: bool = False
    last_observer_poll: Optional[float] = None
    poll_count: int = 0
    uptime_seconds: float = 0.0

    @property
    def tiers_dict(self) -> Dict[str, Dict[str, Any]]:
        """Convert list-format tiers to dict keyed by tier ID."""
        if isinstance(self.tiers, dict):
            return self.tiers
        result = {}
        if isinstance(self.tiers, list):
            for entry in self.tiers:
                if isinstance(entry, dict) and "tier" in entry:
                    tid = entry["tier"]
                    result[tid] = {
                        "status": entry.get("status", "offline"),
                        "detail": entry.get("detail", entry.get("model", "")),
                    }
        return result


class ModeResponse(BaseModel):
    confirmed_mode: OperatingMode
    candidate_mode: Optional[OperatingMode] = None
    candidate_since: Optional[float] = None
    debounce_remaining: Optional[float] = None
    tiers: Dict[str, TierInfo]
    gpu_offload: bool = False
    gpu_detail: Optional[str] = None
    bifrost_profile: Optional[str] = None
    forge_profile: Optional[str] = None
    suggested_bifrost_profile: Optional[str] = None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ArbiterState:
    """All mutable state in one place."""

    def __init__(self) -> None:
        self.confirmed_mode: OperatingMode = OperatingMode.DEGRADED
        self.candidate_mode: Optional[OperatingMode] = None
        self.candidate_since: Optional[float] = None
        self.broadcaster_connected: bool = False
        self.broadcaster: BroadcasterSnapshot = BroadcasterSnapshot()
        self.transitions: deque[ConfirmedTransition] = deque(maxlen=MAX_TRANSITIONS)
        self.poll_count: int = 0
        self.last_poll_at: Optional[float] = None
        self.start_time: float = time.time()


state = ArbiterState()


# ---------------------------------------------------------------------------
# Tier filtering
# ---------------------------------------------------------------------------

def get_tiers_for_mode(mode: OperatingMode) -> Dict[str, TierInfo]:
    """Return only the tiers allowed by policy for the given mode.

    Tiers outside the mode's allowed set are omitted entirely,
    so the Router never sees tiers it shouldn't route to.
    """
    allowed = MODE_TIER_POLICY.get(mode, {"3-Claude", "3-Gemini", "3-Groq"})
    broadcaster_tiers = state.broadcaster.tiers_dict
    result: Dict[str, TierInfo] = {}

    for tier_id in sorted(allowed):
        if tier_id in broadcaster_tiers:
            raw = broadcaster_tiers[tier_id]
            if isinstance(raw, dict):
                result[tier_id] = TierInfo(**raw)
            else:
                result[tier_id] = TierInfo(status="healthy", detail=str(raw))
        else:
            result[tier_id] = TierInfo(status="offline", detail="not reported")

    return result


# ---------------------------------------------------------------------------
# Debounce logic
# ---------------------------------------------------------------------------

def process_mode_update(reported_mode: OperatingMode) -> None:
    """Core state machine: debounce mode transitions."""
    now = time.time()

    # Rule 1: reported matches confirmed → system is stable, clear candidate
    if reported_mode == state.confirmed_mode:
        if state.candidate_mode is not None:
            logger.debug(
                "Candidate %s cleared — Broadcaster reverted to %s",
                state.candidate_mode.value,
                state.confirmed_mode.value,
            )
        state.candidate_mode = None
        state.candidate_since = None
        return

    # Rule 2: new candidate (no existing candidate)
    if state.candidate_mode is None:
        logger.info(
            "New candidate mode: %s (current: %s, debounce: %ds)",
            reported_mode.value,
            state.confirmed_mode.value,
            DEBOUNCE_SECONDS,
        )
        state.candidate_mode = reported_mode
        state.candidate_since = now
        return

    # Rule 4: reported differs from current candidate → flicker, reset
    if reported_mode != state.candidate_mode:
        logger.info(
            "Mode flicker: candidate reset %s → %s",
            state.candidate_mode.value,
            reported_mode.value,
        )
        state.candidate_mode = reported_mode
        state.candidate_since = now
        return

    # Rule 3: reported matches candidate — check if debounce elapsed
    elapsed = now - state.candidate_since
    if elapsed >= DEBOUNCE_SECONDS:
        _confirm_transition(reported_mode, elapsed)
    else:
        logger.debug(
            "Debounce in progress: %s for %.1fs / %ds",
            reported_mode.value,
            elapsed,
            DEBOUNCE_SECONDS,
        )


def _confirm_transition(new_mode: OperatingMode, debounce_duration: float) -> None:
    """Confirm a mode transition after debounce completes."""
    old_mode = state.confirmed_mode
    suggested = MODE_PROFILE_SUGGESTION.get(new_mode)

    transition = ConfirmedTransition(
        timestamp=time.time(),
        from_mode=old_mode,
        to_mode=new_mode,
        debounce_duration=round(debounce_duration, 2),
        trigger=f"Broadcaster reported {new_mode.value} consistently for {debounce_duration:.1f}s",
        suggested_profile=suggested,
    )

    state.transitions.append(transition)
    state.confirmed_mode = new_mode
    state.candidate_mode = None
    state.candidate_since = None

    logger.info(
        "MODE CONFIRMED: %s → %s (debounce: %.1fs, profile suggestion: %s)",
        old_mode.value,
        new_mode.value,
        debounce_duration,
        suggested,
    )


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _poll_loop(client: httpx.AsyncClient) -> None:
    """Continuously poll the Broadcaster and update state."""
    while True:
        try:
            resp = await client.get(
                f"{BROADCASTER_URL}/system/status",
                timeout=PROBE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            snapshot = BroadcasterSnapshot(**data)

            state.broadcaster = snapshot
            state.broadcaster_connected = True
            state.poll_count += 1
            state.last_poll_at = time.time()

            # Run debounce state machine
            try:
                reported = OperatingMode(snapshot.mode)
            except ValueError:
                logger.warning("Unknown mode from Broadcaster: %s", snapshot.mode)
                reported = OperatingMode.DEGRADED

            process_mode_update(reported)

            logger.debug(
                "Poll #%d: mode=%s, tiers=%d, connected=%s",
                state.poll_count,
                snapshot.mode,
                len(snapshot.tiers),
                snapshot.observer_connected,
            )

        except (httpx.HTTPError, httpx.RequestError, Exception) as exc:
            if state.broadcaster_connected:
                logger.warning("Broadcaster fetch failed: %s", exc)
            state.broadcaster_connected = False

        # Always sleep — never tight-loop on error
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start polling task, clean up on shutdown."""
    client = httpx.AsyncClient()
    poll_task = asyncio.create_task(_poll_loop(client))

    logger.info("=" * 60)
    logger.info("BIFROST Arbiter starting")
    logger.info("  Broadcaster: %s", BROADCASTER_URL)
    logger.info("  Poll interval: %ds", POLL_INTERVAL)
    logger.info("  Debounce: %ds", DEBOUNCE_SECONDS)
    logger.info("  Initial mode: %s", state.confirmed_mode.value)
    logger.info("=" * 60)

    yield

    logger.info("Arbiter shutting down — cancelling poll loop")
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await client.aclose()


app = FastAPI(title="BIFROST Arbiter", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/mode")
async def get_mode() -> ModeResponse:
    """Primary endpoint the Router calls — confirmed mode + filtered tiers."""
    now = time.time()

    debounce_remaining = None
    if state.candidate_mode is not None and state.candidate_since is not None:
        debounce_remaining = round(
            max(0.0, DEBOUNCE_SECONDS - (now - state.candidate_since)), 1
        )

    return ModeResponse(
        confirmed_mode=state.confirmed_mode,
        candidate_mode=state.candidate_mode,
        candidate_since=state.candidate_since,
        debounce_remaining=debounce_remaining,
        tiers=get_tiers_for_mode(state.confirmed_mode),
        gpu_offload=state.broadcaster.gpu_offload,
        gpu_detail=state.broadcaster.gpu_detail,
        bifrost_profile=state.broadcaster.bifrost_profile,
        forge_profile=state.broadcaster.forge_profile,
        suggested_bifrost_profile=MODE_PROFILE_SUGGESTION.get(state.confirmed_mode),
    )


@app.get("/transitions")
async def get_transitions() -> List[ConfirmedTransition]:
    """Confirmed mode transition history — newest first."""
    return list(reversed(state.transitions))


@app.get("/health")
async def health():
    """Arbiter health — reports cached state only, no live HTTP calls."""
    now = time.time()
    poll_age = (
        round(now - state.last_poll_at, 1)
        if state.last_poll_at is not None
        else None
    )

    return {
        "status": _derive_health_status(poll_age),
        "confirmed_mode": state.confirmed_mode.value,
        "broadcaster_connected": state.broadcaster_connected,
        "poll_count": state.poll_count,
        "last_poll_age_seconds": poll_age,
        "debouncing": state.candidate_mode is not None,
        "uptime_seconds": round(now - state.start_time, 1),
    }


@app.get("/signals")
async def get_signals():
    """Pass-through of raw Broadcaster signals for debugging."""
    return {
        **state.broadcaster.signals,
        "fetched_at": state.last_poll_at,
    }


# ---------------------------------------------------------------------------
# Health derivation
# ---------------------------------------------------------------------------

def _derive_health_status(poll_age: Optional[float]) -> str:
    """Determine Arbiter health from connectivity and freshness."""
    if not state.broadcaster_connected:
        return "degraded"
    if poll_age is not None and poll_age > (POLL_INTERVAL * 5):
        return "stale"
    if state.poll_count == 0:
        return "starting"
    return "healthy"
