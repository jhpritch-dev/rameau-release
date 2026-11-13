# arbiter.py
# Dependencies: fastapi, uvicorn, httpx, pydantic
# Install: pip install fastapi uvicorn httpx pydantic
# Run: uvicorn arbiter:app --host 0.0.0.0 --port 8082

import asyncio
import datetime
import json
import logging
import os
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = int(os.getenv("ARBITER_POLL_INTERVAL", "5"))
BROADCASTER_URL = os.getenv("BROADCASTER_URL", "http://192.168.x.4:8092")
DEBOUNCE_SECONDS = int(os.getenv("ARBITER_DEBOUNCE_SECONDS", "30"))
PROBE_TIMEOUT = int(os.getenv("ARBITER_PROBE_TIMEOUT", "5"))
FORCE_LOCK_SECONDS = int(os.getenv("ARBITER_FORCE_LOCK_SECONDS", "120"))
MAX_TRANSITIONS = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arbiter")
log = logger

# PACKET-71 F6: the two broadcaster-tier warnings in _normalize_broadcaster_tiers fire once per poll
# (POLL_INTERVAL, 5 s by default), so a condition that persists writes ~17k identical lines a day and
# buries everything else. should_warn keeps the first line of a run and one an hour after that, and
# lets a CHANGED message through at once -- a new tier name must not wait out the interval.
# should_warn was written by lane C (C78, 2026-09-19) and graded 4/4 on CC's seed after exit.
WARN_REPEAT_SECONDS = int(os.getenv("ARBITER_WARN_REPEAT_SECONDS", "3600"))
_WARN_STATE: Dict[str, Any] = {}


def should_warn(message, last_message, last_at, now, interval_s=3600.0):
    """Return True when this warning should be logged now.
    message is None -> False.
    message differs from last_message -> True.
    last_at is None -> True.
    Otherwise True only when now - last_at >= interval_s.
    """
    if message is None:
        return False
    if message != last_message:
        return True
    if last_at is None:
        return True
    return now - last_at >= interval_s


def _warn_once(key: str, message: str, interval_s: float = None,
               level: int = logging.WARNING) -> None:
    """logger.log(level), but at most once per interval for an unchanged message under this key."""
    last_message, last_at = _WARN_STATE.get(key, (None, None))
    now = time.monotonic()
    if should_warn(message, last_message, last_at, now,
                   WARN_REPEAT_SECONDS if interval_s is None else interval_s):
        logger.log(level, "%s", message)
        _WARN_STATE[key] = (message, now)


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
    JARVIS_OFFLINE = "JARVIS_OFFLINE"   # full fleet, zero cloud egress
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
        "1a-coder", "1a-instruct", "1b", "2", "2.5",
        "3-claude", "3-gemini", "3-groq",
        "E-base", "E-local", "1a-hearth",
    },
    OperatingMode.WORKSHOP: {
        "1a-coder", "1a-instruct",
        "3-claude", "3-gemini", "3-groq",
        "E-base", "1a-hearth",
    },
    OperatingMode.CLOUD_PLUS: {
        "3-claude", "3-gemini", "3-groq",
        "E-base", "1a-hearth",
    },
    OperatingMode.WORKSTATION: {
        "1b", "2", "2.5",
        "3-claude", "3-gemini", "3-groq",
        "E-base", "E-local", "1a-hearth",
    },
    OperatingMode.REMOTE: {
        "1a-coder", "1a-instruct", "1b", "2", "2.5",
        "3-claude", "3-gemini", "3-groq",
        "E-base", "E-local", "1a-hearth",
    },
    OperatingMode.NOMAD: {
        "1b", "2", "2.5",
        "3-claude", "3-gemini", "3-groq",
    },
    OperatingMode.WORKSHOP_OFFLINE: {
        "1a-coder", "1a-instruct",
        "E-base", "1a-hearth",
    },
    OperatingMode.JARVIS_OFFLINE: {
        "1a-coder", "1a-instruct", "1b", "2", "2.5",
        "E-base", "E-local", "1a-hearth",
    },
    OperatingMode.DEGRADED: {
        "3-claude", "3-gemini", "3-groq",
    },
}

# --- #57 (2026-08-10): reconcile the broadcaster's tier payload with our own namespace --------
# The broadcaster emits `tiers` as a LIST of {tier,status,model,machine,detail}; this module
# indexed it as a DICT, so every tier read "offline / not reported" -- all 11, for months.
# Case-folding the policy keys resolves "3-Claude"/"3-Gemini"; the one explicit alias resolves
# the "3-groq" -> "3-Fast" rename. E-base/E-local round-trip because the table is built FROM the
# policy keys themselves.
# 2026-09-21: this policy called the Hearth dGPU tier "hearth-gpu", and nothing else does.
# The broadcaster emits "1a-hearth", the router advertises it in /status, and this repo uses
# "1a-hearth" 310 times across 75 files against 15 for the old name. The result was an hourly
# `tier(s) absent from MODE_TIER_POLICY: 1a-hearth` warning -- rate-limited on 2026-09-19
# rather than resolved. Renamed rather than aliased: an alias silences the warning and leaves
# two names for one tier, which is the condition that produced it.
_CANON_TIER = {k.casefold(): k for s in MODE_TIER_POLICY.values() for k in s}
_CANON_TIER.update({
    "3-fast": "3-groq",   # broadcaster advertises the Groq leaf as "3-Fast"
})

# 2026-09-21: the broadcaster advertises the Hearth dGPU TWICE -- once as "1a-hearth"
# (qwen2.5-coder:7b-ctx4k) and once as legacy "hearth-gpu" (qwen2.5-coder:7b), same
# machine. Renaming the policy to "1a-hearth" did not end the unknown-tier warning, it
# only changed which of the two names it named. Dropped rather than aliased: an alias
# maps both onto one key and `out[canon] = entry` would silently keep whichever arrived
# last, losing the other entry's status and model. A drop keeps the survivor intact and
# leaves the alarm armed for every other unknown name. Remove this once the broadcaster
# stops emitting the duplicate.
_LEGACY_TIERS = frozenset({"hearth-gpu"})


def _normalize_broadcaster_tiers(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Accept EITHER broadcaster shape and return a name-keyed dict in OUR namespace.

    Both shapes are handled on purpose: a future broadcaster reverting to a dict must not
    silently re-break this view, which is exactly how #57 stayed invisible.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, list):
        pairs = [(str(e.get("tier", "")), e) for e in raw if isinstance(e, dict)]
    elif isinstance(raw, dict):
        pairs = [(str(k), v if isinstance(v, dict) else {"status": str(v)})
                 for k, v in raw.items()]
    else:
        pairs = []
    dropped = []
    for name, entry in pairs:
        if not name:
            continue
        canon = _CANON_TIER.get(name.casefold(), name.casefold())
        if canon in _LEGACY_TIERS:
            dropped.append(canon)      # deliberate, and said out loud below
            continue
        out[canon] = entry
    # THE ALARM. Its absence is the actual reason this defect survived: `tiers: Any` plus
    # extra=ignore turned a ValidationError into silence. Fail loud, but do not crash the poll.
    if dropped:
        _warn_once("tiers-legacy",
                   "ignoring legacy broadcaster tier(s): %s"
                   % ", ".join(sorted(set(dropped))), level=logging.INFO)
    if raw and not out and not dropped:
        _warn_once("tiers-unparsable",
                   "broadcaster tiers unparsable (type=%s) -- tier view will read offline"
                   % type(raw).__name__)
    else:
        _unknown = sorted(n for n in out if not any(n in s for s in MODE_TIER_POLICY.values()))
        if _unknown:
            # This is what would have surfaced the 3-Fast rename the day it happened.
            _warn_once("tiers-unknown",
                       "broadcaster reports tier(s) absent from MODE_TIER_POLICY: %s"
                       % ", ".join(_unknown))
    return out


MODE_PROFILE_SUGGESTION: Dict[OperatingMode, str] = {
    OperatingMode.JARVIS: "B-Light",
    OperatingMode.WORKSHOP: "B-Dual",
    OperatingMode.CLOUD_PLUS: "B-Light",
    OperatingMode.WORKSTATION: "B-Light",
    OperatingMode.REMOTE: "B-Light",
    OperatingMode.NOMAD: "B-Light",
    OperatingMode.WORKSHOP_OFFLINE: "B-Heavy",
    OperatingMode.JARVIS_OFFLINE: "B-Light",
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
    mode: OperatingMode = OperatingMode.DEGRADED
    tiers: Any = {}
    signals: Any = {}
    gpu_offload: bool = False
    gpu_detail: Optional[str] = None
    bifrost_profile: Optional[str] = None
    forge_profile: Optional[str] = None
    observer_connected: bool = False
    last_observer_poll: Optional[float] = None
    poll_count: int = 0
    uptime_seconds: float = 0.0
    model_config = {"extra": "ignore"}


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
        self.forced_until: float = 0.0  # epoch time until which POST /mode override holds
        # #57: broadcaster tiers, normalized to OUR namespace at poll time.
        self.tiers_by_name: Dict[str, Dict[str, Any]] = {}


state = ArbiterState()


# ---------------------------------------------------------------------------
# Tier filtering
# ---------------------------------------------------------------------------

def get_tiers_for_mode(mode: OperatingMode) -> Dict[str, TierInfo]:
    """Return only the tiers allowed by policy for the given mode.

    Tiers outside the mode's allowed set are omitted entirely,
    so the Router never sees tiers it shouldn't route to.
    """
    allowed = MODE_TIER_POLICY.get(mode, {"3-claude", "3-gemini", "3-groq"})
    result: Dict[str, TierInfo] = {}

    for tier_id in sorted(allowed):
        # #57: read the normalized map, and build TierInfo EXPLICITLY. The old `TierInfo(**raw)`
        # splat was also latently wrong -- broadcaster entries carry tier/model/machine, which
        # are not TierInfo fields, so the splat would have raised the moment the shape was fixed.
        entry = state.tiers_by_name.get(tier_id)
        if isinstance(entry, dict):
            result[tier_id] = TierInfo(status=str(entry.get("status") or "offline"),
                                       detail=entry.get("detail"))
        else:
            result[tier_id] = TierInfo(status="offline", detail="not reported")

    return result


# ---------------------------------------------------------------------------
# Debounce logic
# ---------------------------------------------------------------------------

def process_mode_update(reported_mode: OperatingMode) -> None:
    """Core state machine: debounce mode transitions."""
    now = time.time()

    # If a manual override is active, ignore Broadcaster reports until lock expires
    if now < state.forced_until:
        remaining = round(state.forced_until - now, 0)
        logger.debug("Force lock active (%.0fs remaining) — ignoring Broadcaster report of %s", remaining, reported_mode.value)
        return

    # If a manual override is active, ignore Broadcaster reports until lock expires
    if now < state.forced_until:
        remaining = round(state.forced_until - now, 0)
        logger.debug("Force lock active (%.0fs remaining) — ignoring Broadcaster report of %s", remaining, reported_mode.value)
        return

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
            state.tiers_by_name = _normalize_broadcaster_tiers(snapshot.tiers)   # #57
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

    logger.info("Arbiter shutting down — canceling poll loop")
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await client.aclose()


from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(title="BIFROST Arbiter", lifespan=lifespan)
# 2026-10-06: was allow_origins=["*"] with methods and headers also ["*"], which let ANY
# page a LAN browser visited issue CROSS-ORIGIN WRITES here. POST /mode sets the fleet
# operating mode that the Router polls and follows; POST /board/tasks and the n8n
# activate/deactivate writes sit on the same unauthenticated surface.
# Narrowing is safe here, measured not assumed: every real consumer of :8082 is
# SERVER-SIDE python, where CORS does not apply -- config.py:284 (the Router),
# broadcaster.py:12, commands.py:33. The only browser client is the portal, and it does
# NOT call this service: BifrostPortal.jsx:293/:539 record it unreachable through the
# k3d serverlb and return a stub. This app serves its own /mobile view, which is
# same-origin, and CORS does not apply same-origin.
# THIS IS NOT AUTHENTICATION. It closes the drive-by-browser vector only -- any LAN host
# with curl can still POST here. That gap is tracked separately and is an operator call.
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["GET"], allow_headers=[])


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




@app.post("/mode")
async def set_mode(request: Request):
    from fastapi import Request as _Req
    from fastapi.responses import JSONResponse
    body = await request.json()
    mode_str = body.get("mode", "").upper()
    try:
        new_mode = OperatingMode(mode_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": f"Invalid mode: {mode_str}"})
    old_mode = state.confirmed_mode
    state.confirmed_mode = new_mode
    state.candidate_mode = None
    state.candidate_since = None
    state.forced_until = time.time() + FORCE_LOCK_SECONDS
    logger.info("MODE OVERRIDE: %s -> %s (via POST /mode, locked 10min)", old_mode.value, new_mode.value)
    state.transitions.append(ConfirmedTransition(
        timestamp=time.time(), from_mode=old_mode, to_mode=new_mode,
        debounce_duration=0.0, trigger="Manual override via POST /mode",
    ))
    return {"previous": old_mode.value, "current": new_mode.value}

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




# ---------------------------------------------------------------------------
# Adoption D: phone pairing (agtx design, 2026-09-21)
# ---------------------------------------------------------------------------
# The pairing URL is built from the REQUEST's Host header, never from a configured
# address. Reach the arbiter over the tailnet and the QR carries the tailnet address;
# reach it over the LAN and it carries the LAN one. Two reasons: no fleet address ends
# up in bifrost-platform, which is a PUBLIC repo, and there is no config to drift.
#
# The k3d serverlb maps the arbiter's NodePort to host port 8082, so the tailnet
# address already reaches it. Adoption D needs no new port and no new exposure.

def _qr_svg(data, box=6, quiet=2):
    """QR as inline SVG.

    Returns None when `qrcode` is unavailable. The import is deliberately lazy: a
    missing optional dependency must never stop the arbiter from starting, and this
    endpoint is a convenience, not part of the mode contract.
    """
    try:
        import qrcode
    except Exception:
        _warn_once("qr-missing",
                   "qrcode not installed; /mobile/pair returns a link without an image",
                   level=logging.INFO)
        return None
    q = qrcode.QRCode(border=quiet, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(data)
    q.make(fit=True)
    matrix = q.get_matrix()
    n = len(matrix)
    side = n * box
    rects = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                run = 1
                while x + run < n and row[x + run]:
                    run += 1
                rects.append('<rect x="{0}" y="{1}" width="{2}" height="{3}"/>'.format(
                    x * box, y * box, run * box, box))
                x += run
            else:
                x += 1
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{0}" '
        'viewBox="0 0 {0} {0}" shape-rendering="crispEdges" role="img" '
        'aria-label="pairing QR code">'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        '<g fill="#000000">{1}</g></svg>'
    ).format(side, "".join(rects))


def _pair_host(request):
    """However the caller reached us is how the phone should reach us."""
    host = (request.headers.get("host") or "").strip()
    if not host:
        host = "127.0.0.1:8082"
    bare = host.split(":")[0]
    if bare.startswith("100."):
        network = "tailscale"
    elif bare in ("127.0.0.1", "localhost"):
        network = "loopback"
    else:
        network = "lan"
    return host, network


@app.get("/mobile/pair")
async def mobile_pair(request: Request):
    """Pairing QR for the phone view.

    Scan from a device on the same tailnet. If this was reached over loopback the
    URL will say so -- a loopback QR is useless on a phone, and saying that is more
    helpful than silently handing over an address that cannot work.
    """
    host, network = _pair_host(request)
    url = "http://{0}/mobile".format(host)
    svg = _qr_svg(url)
    return {
        "url": url,
        "network": network,
        "usable_from_phone": network in ("tailscale", "lan"),
        "qr_svg": svg,
        "qr_available": svg is not None,
    }


MOBILE_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>BIFROST Mission Control</title>
<style>
:root{--bg:#0e1116;--surf:#161b22;--bord:#2a3138;--tx:#d7dde4;--txd:#7d8894;
--amb:#d9a441;--grn:#4ec9a0;--red:#e06c60;--mono:ui-monospace,"JetBrains Mono",Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font-family:var(--mono);font-size:13px;
padding:max(12px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom))}
.wrap{max-width:460px;margin:0 auto}
h1{font-size:12px;color:var(--txd);font-weight:500;letter-spacing:.08em;text-transform:uppercase;
margin:0 0 4px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.mode{font-size:26px;margin:2px 0 2px;letter-spacing:-.01em}
.sub{color:var(--txd);font-size:11px;margin-bottom:18px}
.card{background:var(--surf);border:1px solid var(--bord);border-radius:8px;
padding:12px 14px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;
padding:9px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.row:last-child{border-bottom:0}
.nm{font-size:12px}.dt{font-size:10px;color:var(--txd);margin-top:2px}
.pill{font-size:10px;padding:3px 9px;border-radius:999px;white-space:nowrap}
.ok{background:rgba(78,201,160,.15);color:var(--grn)}
.warn{background:rgba(217,164,65,.15);color:var(--amb)}
.bad{background:rgba(224,108,96,.15);color:var(--red)}
.lbl{font-size:10px;color:var(--txd);text-transform:uppercase;letter-spacing:.08em;
margin:18px 0 7px}
.note{color:var(--txd);font-size:10px;line-height:1.5;margin-top:16px;
border-top:1px solid var(--bord);padding-top:12px}
button{font:inherit;font-size:11px;background:transparent;color:var(--txd);
border:1px solid var(--bord);border-radius:6px;padding:6px 12px;min-height:34px}
</style></head><body><div class="wrap">
<h1><span>bifrost &middot; mission control</span><button onclick="load()">refresh</button></h1>
<div class="mode" id="mode">...</div>
<div class="sub" id="sub">connecting</div>
<div id="body"></div>
<div class="note" id="note"></div>
</div><script>
var esc=function(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){
return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})};
function pill(s){var c="warn";s=String(s||"");
if(/healthy|available|ok|success/i.test(s))c="ok";
if(/offline|error|fail|degraded|not reported/i.test(s))c="bad";
return '<span class="pill '+c+'">'+esc(s)+'</span>'}
async function load(){
 try{
  var m=await (await fetch("/mode",{cache:"no-store"})).json();
  document.getElementById("mode").textContent=m.confirmed_mode||"?";
  var t=m.tiers||{},k=Object.keys(t).sort();
  document.getElementById("sub").textContent=
    k.length+" tiers"+(m.bifrost_profile?" \\u00b7 "+m.bifrost_profile:"")+
    (m.debounce_remaining!=null?" \\u00b7 debounce "+m.debounce_remaining+"s":"");
  var h='<div class="lbl">tiers</div><div class="card">';
  k.forEach(function(n){var v=t[n]||{};
    h+='<div class="row"><div><div class="nm">'+esc(n)+'</div>'+
       (v.model?'<div class="dt">'+esc(v.model)+'</div>':'')+
       '</div>'+pill(v.status)+'</div>'});
  h+='</div>';
  try{
   var tk=await (await fetch("/tasks",{cache:"no-store"})).json();
   var rows=(tk.workflows||tk.tasks||[]).slice(0,8);
   if(rows.length){h+='<div class="lbl">recent work</div><div class="card">';
    rows.forEach(function(r){
      h+='<div class="row"><div><div class="nm">'+esc(r.name||r.id||"task")+'</div>'+
         (r.last_run||r.started?'<div class="dt">'+esc(r.last_run||r.started)+'</div>':'')+
         '</div>'+pill(r.status||r.state)+'</div>'});
    h+='</div>'}
  }catch(e){}
  document.getElementById("body").innerHTML=h;
  document.getElementById("note").textContent=
    "Read-only. Approving agent prompts from the phone needs a session API the arbiter "+
    "does not expose yet \\u2014 that lands with Adoptions A and B.";
 }catch(e){
  document.getElementById("sub").textContent="unreachable \\u2014 "+e;
 }
}
load();setInterval(load,15000);
</script></body></html>
"""


@app.get("/mobile")
async def mobile_board():
    """Self-contained phone view.

    Served by the arbiter itself rather than as a Portal route: the QR points at
    :8082, and the arbiter cannot serve the Portal's React bundle. No external
    resources, so it works on a phone with nothing but tailnet access.
    """
    from fastapi.responses import HTMLResponse
    return HTMLResponse(MOBILE_PAGE)



# ---------------------------------------------------------------------------
# Adoptions A + B: coordination board with a dependency graph (agtx, 2026-09-21)
# ---------------------------------------------------------------------------
# Path comes from the environment, NOT a literal. The brief had
# COORD_BASE = Path(r"F:\Documents\AI_Cloud\Inhouse\Coordination") inside this
# module -- but this module runs in a Linux container with no F: drive. The
# /inbox and /tasks mounts are the existing precedent and this follows them:
# hostPath L:\net-ssd\bifrost-board -> /board, read-write.
#
# Five more defects in the briefed design are fixed here rather than shipped:
#   1. a dependency naming a task that does not exist silently blocked the
#      dependent task FOREVER -- `.get(d, {}).get("phase") == "done"` is False
#      for a typo, with no signal anywhere. Unknown deps are now reported.
#   2. a dependency CYCLE also blocked silently. Cycles are detected and named.
#   3. save() was a plain write -- an interrupted write truncates the board.
#      Now: write a temp file in the same directory, then os.replace (atomic).
#   4. read-modify-write with no serialization loses concurrent updates. A
#      process-wide lock covers the arbiter's own concurrency.
#   5. `return {...}, 404` does NOT set a 404 in FastAPI -- it serializes the
#      tuple into a 200 body. Real bug. HTTPException is used instead.

from fastapi import HTTPException

BOARD_DIR = os.getenv("BOARD_DIR", "/board")
BOARD_FILE = os.path.join(BOARD_DIR, "board.json")
BOARD_PHASES = ("pending", "running", "review", "done", "blocked")
# The whole board is rewritten on every update, so an unbounded brief body
# would make every PATCH cost the size of the largest brief ever filed.
BOARD_BRIEF_MAX = int(os.getenv("BOARD_BRIEF_MAX", "65536"))
_BOARD_LOCK = threading.Lock()


def _board_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _board_empty():
    return {"tasks": {}, "updated": None}


def _board_load():
    """Never raise. A missing board is empty; a corrupt one is reported, not swallowed."""
    try:
        with open(BOARD_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _board_empty()
    except (ValueError, OSError) as exc:
        _warn_once("board-unreadable", "board.json unreadable (%s); serving empty" % exc)
        return _board_empty()
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
        _warn_once("board-malformed", "board.json is not a task map; serving empty")
        return _board_empty()
    return data


def _board_save(board):
    """Atomic: temp file in the SAME directory, then replace. os.replace is atomic
    on both POSIX and Windows, and a same-directory temp guarantees same filesystem."""
    board["updated"] = _board_now()
    os.makedirs(BOARD_DIR, exist_ok=True)
    tmp = BOARD_FILE + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(board, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, BOARD_FILE)
    return board


def _board_cycles(tasks):
    """Every dependency cycle, as lists of task ids. Iterative DFS -- a deep graph
    must not blow the stack inside a request handler."""
    cycles = []
    seen = set()
    for root in sorted(tasks):
        if root in seen:
            continue
        stack = [(root, [root], set([root]))]
        while stack:
            node, path, on_path = stack.pop()
            seen.add(node)
            for dep in (tasks.get(node, {}).get("depends_on") or []):
                if dep not in tasks:
                    continue                      # unknown dep -- reported separately
                if dep in on_path:
                    cyc = path[path.index(dep):]
                    key = tuple(sorted(cyc))
                    if key not in [tuple(sorted(c)) for c in cycles]:
                        cycles.append(cyc)
                    continue
                stack.append((dep, path + [dep], on_path | {dep}))
    return cycles


def _board_analyse(board):
    """Per task: what is unmet, what is unknown, and is it ready.

    'Ready' means pending AND every dependency exists AND every one is done.
    A task depending on something that does not exist is NOT ready and is
    surfaced as such -- the briefed version made that indistinguishable from
    a dependency that is merely unfinished.
    """
    tasks = board.get("tasks") or {}
    cycles = _board_cycles(tasks)
    in_cycle = {t for c in cycles for t in c}
    out = {}
    for tid, task in tasks.items():
        deps = task.get("depends_on") or []
        unknown = [d for d in deps if d not in tasks]
        unmet = [d for d in deps
                 if d in tasks and (tasks[d].get("phase") != "done")]
        out[tid] = {
            "unknown_dependencies": unknown,
            "unmet_dependencies": unmet,
            "in_cycle": tid in in_cycle,
            "ready": (task.get("phase") == "pending"
                      and not unknown and not unmet and tid not in in_cycle),
        }
    return out, cycles


def _board_levels(tasks, cycles):
    """Fan-out levels: level 0 has no dependencies, level n waits on n-1.

    This is Adoption B's actual point -- everything at the same level can run in
    parallel. Tasks in a cycle have no level and are excluded.
    """
    in_cycle = {t for c in cycles for t in c}
    level = {}
    remaining = {t for t in tasks if t not in in_cycle}
    guard = 0
    while remaining and guard <= len(tasks) + 1:
        guard += 1
        progressed = False
        for tid in sorted(remaining):
            deps = [d for d in (tasks[tid].get("depends_on") or [])
                    if d in tasks and d not in in_cycle]
            if all(d in level for d in deps):
                level[tid] = 1 + max([level[d] for d in deps], default=-1)
                progressed = True
        remaining -= set(level)
        if not progressed:
            break                                  # unreachable via unknown deps
    return level


@app.get("/board")
async def board_get():
    """The whole board, with dependency analysis attached."""
    board = _board_load()
    analysis, cycles = _board_analyse(board)
    return {
        "tasks": board.get("tasks", {}),
        "updated": board.get("updated"),
        "analysis": analysis,
        "cycles": cycles,
        "path": BOARD_FILE,
    }


@app.get("/board/ready")
async def board_ready(assigned_to: Optional[str] = None):
    """Tasks that can start now. Igor polls this instead of scanning a folder.

    `blocked` is returned alongside on purpose: a task that can never become
    ready (unknown dependency, or a cycle) must be visible, not merely absent.
    """
    board = _board_load()
    tasks = board.get("tasks") or {}
    analysis, cycles = _board_analyse(board)
    ready = [tasks[t] for t in sorted(tasks) if analysis[t]["ready"]]
    if assigned_to is not None:
        # A worker claims only its own queue. Unassigned work is nobody's by
        # default -- an unassigned task must not be picked up by whoever polls
        # first, or two workers race for it.
        ready = [t for t in ready if t.get("assigned_to") == assigned_to]
    blocked = [
        {"id": t,
         "unknown_dependencies": analysis[t]["unknown_dependencies"],
         "in_cycle": analysis[t]["in_cycle"]}
        for t in sorted(tasks)
        if tasks[t].get("phase") == "pending"
        and (analysis[t]["unknown_dependencies"] or analysis[t]["in_cycle"])
    ]
    return {"ready": ready, "count": len(ready),
            "blocked": blocked, "cycles": cycles}


@app.get("/board/graph")
async def board_graph():
    """Dependency graph as parallel levels -- Adoption B's fan-out view."""
    board = _board_load()
    tasks = board.get("tasks") or {}
    _, cycles = _board_analyse(board)
    level = _board_levels(tasks, cycles)
    levels = {}
    for tid, n in level.items():
        levels.setdefault(str(n), []).append(tid)
    for n in levels:
        levels[n].sort()
    return {"levels": levels,
            "max_level": max(level.values(), default=-1),
            "cycles": cycles,
            "unlevelled": sorted(t for t in tasks if t not in level)}


@app.post("/board/tasks")
async def board_create(body: dict):
    """Create a task. Validates rather than raising KeyError on a bad body."""
    title = (body or {}).get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=422, detail="title is required")
    deps = (body or {}).get("depends_on") or []
    if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
        raise HTTPException(status_code=422, detail="depends_on must be a list of ids")
    brief = (body or {}).get("brief")
    if brief is not None:
        if not isinstance(brief, str):
            raise HTTPException(status_code=422, detail="brief must be a string")
        if len(brief) > BOARD_BRIEF_MAX:
            raise HTTPException(
                status_code=413,
                detail="brief is %d chars; limit is %d" % (len(brief), BOARD_BRIEF_MAX))
    with _BOARD_LOCK:
        board = _board_load()
        tasks = board.setdefault("tasks", {})
        tid = (body.get("id") or "").strip()
        if not tid:
            # Counter-based ids collide after a deletion. Derive from the title
            # and disambiguate against what is actually on the board.
            base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
            tid = base
            n = 2
            while tid in tasks:
                tid = "%s-%d" % (base, n)
                n += 1
        if tid in tasks:
            raise HTTPException(status_code=409, detail="task %s already exists" % tid)
        if tid in deps:
            raise HTTPException(status_code=422, detail="a task cannot depend on itself")
        tasks[tid] = {
            "id": tid,
            "title": title,
            "phase": "pending",
            "depends_on": deps,
            "assigned_to": body.get("assigned_to"),
            "brief_path": body.get("brief_path"),
            "brief": brief,
            "created": _board_now(),
            "updated": _board_now(),
            "result": None,
        }
        _board_save(board)
    return {"task_id": tid, "status": "created",
            "unknown_dependencies": [d for d in deps if d not in tasks]}


@app.patch("/board/tasks/{task_id}")
async def board_update(task_id: str, body: dict):
    """Advance a task. An unknown phase is refused -- a typo must not park a task
    in a state nothing will ever pick up."""
    body = body or {}
    phase = body.get("phase")
    if phase is not None and phase not in BOARD_PHASES:
        raise HTTPException(status_code=422,
                            detail="phase must be one of %s" % (list(BOARD_PHASES),))
    expect = body.get("if_phase")
    if expect is not None and expect not in BOARD_PHASES:
        raise HTTPException(status_code=422,
                            detail="if_phase must be one of %s" % (list(BOARD_PHASES),))
    with _BOARD_LOCK:
        board = _board_load()
        tasks = board.get("tasks") or {}
        if task_id not in tasks:
            raise HTTPException(status_code=404, detail="no such task: %s" % task_id)
        if expect is not None and tasks[task_id].get("phase") != expect:
            # Compare-and-set. Two workers polling /board/ready see the same
            # task; without this the second claim silently overwrites the first.
            raise HTTPException(
                status_code=409,
                detail="task %s is %r, not %r"
                       % (task_id, tasks[task_id].get("phase"), expect))
        for k in ("phase", "result", "assigned_to", "error", "brief_path", "brief"):
            if k in body:
                tasks[task_id][k] = body[k]
        tasks[task_id]["updated"] = _board_now()
        _board_save(board)
        analysis, _ = _board_analyse(board)
    return {"task_id": task_id,
            "phase": tasks[task_id]["phase"],
            "unblocked": [t for t, a in analysis.items() if a["ready"]]}

@app.get("/signals")
async def get_signals():
    """Pass-through of raw Broadcaster signals for debugging."""
    return {
        **state.broadcaster.signals,
        "fetched_at": state.last_poll_at,
    }


@app.get("/quantum")
async def get_quantum_rewards(n: int = 20):
    """Recent SIA quantum-reward (gate_7) scores for the Portal QuantumRewardPanel.

    Reads the shared quantum_rewards.jsonl appended by session_o_gates.gate_7
    (and the standalone test). Mounted into this pod at /mnt/bifrost-logs
    (hostPath /net-ssd/bifrost-logs). Returns most-recent-first.
    """
    import json as _json
    from pathlib import Path as _Path
    log_path = _Path(os.environ.get(
        "QUANTUM_REWARDS_LOG", "/mnt/bifrost-logs/telemetry/quantum_rewards.jsonl"))
    if not log_path.exists():
        return {"available": False, "count": 0, "entries": [], "latest": None}
    try:
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        entries = [_json.loads(ln) for ln in lines[-n:] if ln.strip()]
        entries.reverse()  # most recent first
        return {
            "available": True,
            "count": len(entries),
            "entries": entries,
            "latest": entries[0] if entries else None,
        }
    except Exception as e:
        logger.warning("inbox: unavailable (%s: %s)", type(e).__name__, e)
        return {"available": False, "error": str(e), "count": 0, "entries": [], "latest": None}


# ---------------------------------------------------------------------------
# OpenWorker decision inbox (C1, 2026-08-04). Consumer half of the inbox pattern:
# a suspended decision parks as one JSON file instead of timing out or being auto-decided.
# READ-ONLY by construction -- glob + json.loads, no writes, no shell. The producer
# (igor_tool_loop session-suspension) is a separate PROTECTED ceremony and does not exist
# yet, so this returns an empty list until it lands. That is the designed state.
# Source dir is a hostPath mount of L:\net-ssd\bifrost-inbox on Hearth (the k3d node already
# binds L:\net-ssd -> /net-ssd, so no new node mount was needed).
# ---------------------------------------------------------------------------


@app.get("/inbox")
async def get_inbox(limit: int = 50):
    """Suspended-decision inbox for the Portal InboxPanel.

    Pending items first, then newest by timestamp. Never raises: a missing mount or an
    unreadable file degrades to an empty/partial list so the panel renders rather than errors.
    """
    import json as _json
    from pathlib import Path as _Path

    inbox_dir = _Path(os.environ.get("INBOX_DIR", "/inbox"))
    try:
        if not inbox_dir.exists():
            return {"available": False, "count": 0, "pending": 0, "items": []}
        items = []
        for f in sorted(inbox_dir.glob("*.json")):
            try:
                rec = _json.loads(f.read_text(encoding="utf-8"))
            except Exception as _e:
                # One bad file must not blank the panel -- but say so. Swallowing this silently
                # is the exact failure shape this session spent the day removing.
                logger.warning("inbox: skipping unreadable record %s (%s)", f.name, _e)
                continue
            if isinstance(rec, dict):
                rec.setdefault("id", f.stem)
                rec.setdefault("status", "pending")
                items.append(rec)
        # pending first, then newest timestamp first -- done by the two partition sorts
        # below. A combined items.sort() used to run here; its result was discarded
        # wholesale by the re-partition and re-sort that follow, on every inbox poll.
        pend = [r for r in items if str(r.get("status", "")).lower() == "pending"]
        rest = [r for r in items if str(r.get("status", "")).lower() != "pending"]
        pend.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
        rest.sort(key=lambda r: str(r.get("timestamp", "")), reverse=True)
        ordered = (pend + rest)[: max(1, int(limit))]
        return {
            "available": True,
            "count": len(items),
            "pending": len(pend),
            "items": ordered,
        }
    except Exception as e:
        # Log it. This degrades the Portal InboxPanel to "unavailable"; without a
        # server-side trace a future defect here (NameError, AttributeError, ...) is
        # indistinguishable from an expected I/O failure. The inner handler logs.
        logger.warning("inbox unavailable: %s", e, exc_info=True)
        return {"available": False, "error": str(e), "count": 0, "pending": 0, "items": []}


# ---------------------------------------------------------------------------
# n8n proxy (Phase 3, 2026-07-09) -- READ-ONLY.
# The N8N_API_KEY lives only in this pod's env (wired from secret/bifrost-api-keys);
# the Portal calls /arbiter/n8n/* and NEVER sees the key. GET-only by design:
# a workflow *trigger/execute* (write) surface is a separate, gated decision and is
# intentionally NOT exposed here (see BIFROST-BACKLOG v29 -- n8n Phase-3).
# ---------------------------------------------------------------------------

N8N_URL = os.getenv("N8N_URL", "http://192.168.x.4:5678").rstrip("/")
N8N_API_KEY = os.getenv("N8N_API_KEY", "")
N8N_TIMEOUT = int(os.getenv("N8N_TIMEOUT", "8"))


def _n8n_headers() -> Dict[str, str]:
    return {"X-N8N-API-KEY": N8N_API_KEY, "accept": "application/json"}


@app.get("/n8n/status")
async def n8n_status():
    """Whether the n8n proxy is configured + reachable (never leaks the key)."""
    if not N8N_API_KEY:
        return {"configured": False, "reason": "N8N_API_KEY not set in arbiter env"}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{N8N_URL}/api/v1/workflows", headers=_n8n_headers(),
                            params={"limit": 1}, timeout=N8N_TIMEOUT)
        return {"configured": True, "n8n_url": N8N_URL,
                "reachable": r.status_code == 200, "http_status": r.status_code,
                "write_enabled": N8N_WRITE_ENABLED,
                "run_configured": bool(N8N_RUNNOW_WEBHOOK),
                "run_allowlist": sorted(N8N_RUN_ALLOWLIST)}
    except Exception as e:
        return {"configured": True, "n8n_url": N8N_URL, "reachable": False, "error": str(e)[:120],
                "write_enabled": N8N_WRITE_ENABLED}


@app.get("/n8n/workflows")
async def n8n_workflows():
    """List n8n workflows (id, name, active, updatedAt) -- slim, read-only proxy."""
    if not N8N_API_KEY:
        return {"error": "N8N_API_KEY not configured", "data": [], "count": 0}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{N8N_URL}/api/v1/workflows", headers=_n8n_headers(), timeout=N8N_TIMEOUT)
        if r.status_code != 200:
            return {"error": f"n8n returned {r.status_code}", "data": [], "count": 0}
        raw = r.json()
        items = raw.get("data", raw) if isinstance(raw, dict) else raw
        slim = [{"id": w.get("id"), "name": w.get("name"), "active": w.get("active"),
                 "updatedAt": w.get("updatedAt")} for w in (items or [])]
        return {"data": slim, "count": len(slim)}
    except Exception as e:
        return {"error": str(e)[:160], "data": [], "count": 0}


@app.get("/n8n/executions/{workflow_id}")
async def n8n_last_execution(workflow_id: str, limit: int = 1):
    """Recent executions for a workflow (status/startedAt) -- slim, read-only proxy."""
    if not N8N_API_KEY:
        return {"error": "N8N_API_KEY not configured", "data": [], "count": 0}
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{N8N_URL}/api/v1/executions", headers=_n8n_headers(),
                            params={"workflowId": workflow_id, "limit": max(1, min(limit, 20))},
                            timeout=N8N_TIMEOUT)
        if r.status_code != 200:
            return {"error": f"n8n returned {r.status_code}", "data": [], "count": 0}
        raw = r.json()
        items = raw.get("data", raw) if isinstance(raw, dict) else raw
        slim = [{"id": e.get("id"), "finished": e.get("finished"), "status": e.get("status"),
                 "startedAt": e.get("startedAt"), "stoppedAt": e.get("stoppedAt"),
                 "mode": e.get("mode")} for e in (items or [])]
        return {"data": slim, "count": len(slim), "workflowId": workflow_id}
    except Exception as e:
        return {"error": str(e)[:160], "data": [], "count": 0}


# ---------------------------------------------------------------------------
# /tasks (W1, 2026-09-18, John: "build") -- N8N-MIGRATION-DESIGN-2026-07-07 Branch 5: "last execution
# time + result for each registered task". READ-ONLY. The logic is arbiter_tasks.build_tasks (Lane B,
# HW1r 19/19); this is only the wiring. n8n workflows + their last execution; plus an optional Windows
# scheduled-task snapshot (this pod cannot see Task Scheduler) that reads couldnt_check when absent/stale.
# ---------------------------------------------------------------------------

TASKS_SNAPSHOT_PATH = os.getenv("TASKS_SNAPSHOT_PATH", "")
TASKS_SNAPSHOT_MAX_AGE_S = int(os.getenv("TASKS_SNAPSHOT_MAX_AGE_S", "3600"))


async def _n8n_get(path: str, params: Optional[Dict[str, Any]] = None):
    """(status_code, body) for a read-only n8n API GET. Raises on transport errors (build_tasks maps them)."""
    if not N8N_API_KEY:
        return 503, {"message": "N8N_API_KEY not configured in arbiter env"}
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{N8N_URL}{path}", headers=_n8n_headers(), params=params or None, timeout=N8N_TIMEOUT)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


@app.get("/tasks")
async def get_tasks():
    """Every n8n workflow with its last execution, plus the Windows task snapshot. Never a 500: the
    lane module is called by KEYWORD with `now` always passed (its positional order differs from the
    contract and it does not default `now`), and anything it raises becomes an error field."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    try:
        from arbiter_tasks import build_tasks
        return await build_tasks(_n8n_get, snapshot_path=TASKS_SNAPSHOT_PATH or None, now=now,
                                 max_age_s=TASKS_SNAPSHOT_MAX_AGE_S)
    except Exception as e:
        logger.warning("/tasks: build failed: %s", e, exc_info=True)
        return {"error": f"{type(e).__name__}: {str(e)[:160]}", "generated_at": now.isoformat(),
                "n8n": {"reachable": None, "error": "not evaluated", "tasks": []},
                "windows": {"couldnt_check": True, "reason": "not evaluated", "snapshot_age_s": None, "tasks": []}}


# ---------------------------------------------------------------------------
# n8n WRITE surface (Phase 3.5, 2026-07-09) -- GATED, disabled by default.
# activate/deactivate = API-native (reversible cron toggle). run-now = fire a dedicated
# n8n dispatcher webhook (the public API has NO execute endpoint). Both require
# N8N_WRITE_ENABLED=true on the arbiter; run-now additionally requires the target id to be
# in N8N_RUN_ALLOWLIST and the dispatcher webhook to be configured. Key stays server-side.
# ---------------------------------------------------------------------------

N8N_WRITE_ENABLED = os.getenv("N8N_WRITE_ENABLED", "false").lower() == "true"
N8N_RUNNOW_WEBHOOK = os.getenv("N8N_RUNNOW_WEBHOOK", "")
N8N_RUN_ALLOWLIST = set(x.strip() for x in os.getenv("N8N_RUN_ALLOWLIST", "").split(",") if x.strip())
# Shared secret sent as the X-Bifrost-RunNow header so the dispatcher webhook can require Header-Auth
# (closes the webhook-bypass: a direct unauthenticated POST is then rejected 403 by n8n). Value lives
# only in secret/bifrost-api-keys; if empty, no header is sent (pre-mitigation behavior).
N8N_RUNNOW_SECRET = os.getenv("N8N_RUNNOW_SECRET", "")


def _write_gate():
    if not N8N_WRITE_ENABLED:
        return {"ok": False, "disabled": True,
                "error": "n8n write surface disabled (set N8N_WRITE_ENABLED=true on the arbiter)"}
    if not N8N_API_KEY:
        return {"ok": False, "error": "N8N_API_KEY not configured"}
    return None


async def _n8n_post(path: str, headers=None, json_body=None, url=None):
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(url or f"{N8N_URL}{path}", headers=headers if headers is not None else _n8n_headers(),
                             json=json_body, timeout=N8N_TIMEOUT)
        ct = r.headers.get("content-type", "")
        return {"ok": r.status_code < 300, "http_status": r.status_code,
                "body": (r.json() if ct.startswith("application/json") else r.text[:200])}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


@app.post("/n8n/workflows/{workflow_id}/activate")
async def n8n_activate(workflow_id: str):
    g = _write_gate()
    if g:
        return g
    return await _n8n_post(f"/api/v1/workflows/{workflow_id}/activate")


@app.post("/n8n/workflows/{workflow_id}/deactivate")
async def n8n_deactivate(workflow_id: str):
    g = _write_gate()
    if g:
        return g
    return await _n8n_post(f"/api/v1/workflows/{workflow_id}/deactivate")


@app.post("/n8n/run/{workflow_id}")
async def n8n_run(workflow_id: str):
    """Run-now: fire the n8n run-now dispatcher webhook with the target id. Gated + allowlisted."""
    g = _write_gate()
    if g:
        return g
    if not N8N_RUNNOW_WEBHOOK:
        return {"ok": False, "error": "run-now not configured (N8N_RUNNOW_WEBHOOK unset)"}
    if N8N_RUN_ALLOWLIST and workflow_id not in N8N_RUN_ALLOWLIST:
        return {"ok": False, "error": f"workflow {workflow_id} not in N8N_RUN_ALLOWLIST"}
    hdrs = {"accept": "application/json"}
    if N8N_RUNNOW_SECRET:
        hdrs["X-Bifrost-RunNow"] = N8N_RUNNOW_SECRET
    res = await _n8n_post("", headers=hdrs, json_body={"workflowId": workflow_id}, url=N8N_RUNNOW_WEBHOOK)
    res["workflowId"] = workflow_id
    return res


_RUNNER_NAME = "BIFROST run-now dispatcher (gated)"
_RUNNER_PATH = "bifrost-run-now"


def _runner_workflow_body():
    return {
        "name": _RUNNER_NAME,
        "nodes": [
            {"parameters": {"httpMethod": "POST", "path": _RUNNER_PATH, "responseMode": "onReceived", "options": {}},
             "id": "webhook-runnow", "name": "Run-now Webhook", "type": "n8n-nodes-base.webhook",
             "typeVersion": 2, "position": [420, 300], "webhookId": _RUNNER_PATH},
            {"parameters": {"source": "database", "workflowId": "={{ $json.body.workflowId }}", "options": {}},
             "id": "exec-target", "name": "Execute Target", "type": "n8n-nodes-base.executeWorkflow",
             "typeVersion": 1, "position": [660, 300]},
        ],
        "connections": {"Run-now Webhook": {"main": [[{"node": "Execute Target", "type": "main", "index": 0}]]}},
        "settings": {},
    }


@app.post("/n8n/_bootstrap-runner")
async def n8n_bootstrap_runner():
    """One-shot: create + activate the run-now dispatcher workflow (gated). Idempotent by name.

    Uses the arbiter's server-side key so the operator never has to hand the key to CC. Returns the
    webhook URL to set as N8N_RUNNOW_WEBHOOK. Creating workflows is scoped to THIS fixed dispatcher only.
    """
    g = _write_gate()
    if g:
        return g
    try:
        async with httpx.AsyncClient() as c:
            existing = await c.get(f"{N8N_URL}/api/v1/workflows", headers=_n8n_headers(), timeout=N8N_TIMEOUT)
            found = None
            if existing.status_code == 200:
                for w in (existing.json().get("data", []) or []):
                    if w.get("name") == _RUNNER_NAME:
                        found = w
                        break
            if found:
                wid = found["id"]
                created = False
            else:
                cr = await c.post(f"{N8N_URL}/api/v1/workflows", headers=_n8n_headers(),
                                  json=_runner_workflow_body(), timeout=N8N_TIMEOUT)
                if cr.status_code >= 300:
                    return {"ok": False, "step": "create", "http_status": cr.status_code, "body": cr.text[:300]}
                wid = cr.json().get("id")
                created = True
            act = await c.post(f"{N8N_URL}/api/v1/workflows/{wid}/activate", headers=_n8n_headers(), timeout=N8N_TIMEOUT)
        return {"ok": act.status_code < 300, "workflow_id": wid, "created": created,
                "activate_status": act.status_code,
                "webhook_url": f"{N8N_URL}/webhook/{_RUNNER_PATH}",
                "next": "set N8N_RUNNOW_WEBHOOK to webhook_url on the arbiter + rollout"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


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


