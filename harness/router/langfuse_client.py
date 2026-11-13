"""
langfuse_client.py -- best-effort, env-gated Langfuse tracing via the v2 ingestion API.

Added 2026-06-26 (Langfuse Option A). Emits one Langfuse generation per completed
inference, carrying model / tier / token usage / source attribution. The `source`
metadata (cc_dispatch | session_o | <empty: untagged>) is what replaces the
working-hours heuristic in token_offload_report.py with EXACT source attribution.

Why direct HTTP instead of the langfuse SDK:
  * The langfuse v3 SDK exports via OTLP to /api/public/otel, which the deployed
    Langfuse server (v2.95) returns 404 for (no OTel ingestion on the v2 line).
  * The langfuse v2 SDK pins packaging<25, conflicting with requirements packaging==26.
  * So we POST directly to the v2 server's native /api/public/ingestion endpoint
    using httpx (already a dependency). Proven against the live server (201/201).

Design rules:
  * Fully env-gated. If LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST are
    not all present, emit_generation() is a silent no-op. Safe to deploy before keys.
  * Never raises. A tracing failure must NEVER break an inference response.
  * Non-blocking on the request path. emit_generation is awaited only inside the
    already-fire-and-forget completion-telemetry task, with a short HTTP timeout.
"""
import base64
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_enabled = None        # tri-state: None=unchecked, True/False after first _config()
_host = None
_auth_header = None


def _config() -> bool:
    """Lazily resolve config once. Returns True if tracing is enabled."""
    global _enabled, _host, _auth_header
    if _enabled is not None:
        return _enabled
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    if not (pk and sk and host):
        log.info("[langfuse] keys/host not all present -> tracing DISABLED (no-op)")
        _enabled = False
        return False
    _host = host.rstrip("/")
    _auth_header = "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()
    _enabled = True
    log.info("[langfuse] direct-ingestion tracing ENABLED host=%s", _host)
    return True


def _now_iso() -> str:
    # Z-suffixed ISO8601 (proven accepted by the v2.95 ingestion API).
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def emit_generation(event) -> None:
    """Best-effort: POST one generation to the Langfuse v2 ingestion API. Never raises.

    `event` is a telemetry.InferenceEvent (duck-typed to avoid an import cycle).
    """
    try:
        if not _config():
            return

        now = _now_iso()
        trace_id = str(uuid.uuid4())
        obs_id = str(uuid.uuid4())
        source = (getattr(event, "source", "") or "").strip().lower() or "untagged"

        metadata = {
            "source": source,                       # cc_dispatch | session_o | untagged
            "tier_used": getattr(event, "tier_used", ""),
            "complexity_band": getattr(event, "complexity_band", ""),
            "strategy": getattr(event, "strategy", ""),
            "status": getattr(event, "status", ""),
            "machine": getattr(event, "machine", ""),
            "escalation_count": getattr(event, "escalation_count", 0),
            "cloud_cost_usd": getattr(event, "cloud_cost_usd", 0.0),
        }

        batch = {"batch": [
            {
                "id": str(uuid.uuid4()), "type": "trace-create", "timestamp": now,
                "body": {"id": trace_id, "name": "bifrost-inference",
                         "metadata": {"source": source}},
            },
            {
                "id": str(uuid.uuid4()), "type": "observation-create", "timestamp": now,
                "body": {
                    "id": obs_id, "traceId": trace_id, "type": "GENERATION",
                    "name": "bifrost-inference",
                    "model": getattr(event, "model", "") or "unknown",
                    "startTime": now, "endTime": now,
                    "usage": {
                        "input": int(getattr(event, "tokens_input", 0) or 0),
                        "output": int(getattr(event, "tokens_output", 0) or 0),
                        "total": int(getattr(event, "tokens_total", 0) or 0),
                        "unit": "TOKENS",
                    },
                    "metadata": metadata,
                    "level": "DEFAULT" if getattr(event, "status", "PASS") == "PASS" else "WARNING",
                },
            },
        ]}

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{_host}/api/public/ingestion",
                json=batch,
                headers={"Authorization": _auth_header},
            )
    except Exception as exc:  # noqa: BLE001 -- tracing must never break inference
        log.debug("[langfuse] emit_generation failed: %s", exc)
