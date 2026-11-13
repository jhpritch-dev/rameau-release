"""
BIFROST Router — Metrics
=========================
Dual-mode metrics: in-memory for /status API + Prometheus for Grafana.
Prometheus client exposes /metrics in the standard exposition format.
"""

import time
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
    REGISTRY,
)

from config import ComplexityBand, Tier


# ---------------------------------------------------------------------------
# Prometheus metrics — scraped by Hearth Prometheus
# ---------------------------------------------------------------------------

ROUTER_INFO = Info(
    "bifrost_router",
    "BIFROST Router instance info",
)

REQUESTS_TOTAL = Counter(
    "bifrost_requests_total",
    "Total inference requests routed",
    ["band", "tier", "success"],
)

BAND_TOTAL = Counter(
    "bifrost_band_total",
    "Requests per complexity band",
    ["band"],
)

TIER_TOTAL = Counter(
    "bifrost_tier_total",
    "Requests per inference tier",
    ["tier"],
)

ESCALATIONS_TOTAL = Counter(
    "bifrost_escalations_total",
    "Total escalation events (tier fallback)",
    ["from_tier", "to_tier"],
)

# #87 (2026-08-13): a wedged backend returns HTTP 200 with no usable answer. This counter
# is incremented even when BIFROST_WEDGE_ACTION=log suppresses the refusal, so turning the
# refusal off never turns the evidence off.
BACKEND_WEDGED_TOTAL = Counter(
    "bifrost_backend_wedged_total",
    "Backend returned HTTP 200 with no usable answer (wedged instance)",
    ["endpoint"],
)

CLOUD_REQUESTS = Counter(
    "bifrost_cloud_requests_total",
    "Requests routed to cloud tiers",
    ["tier"],
)

LOCAL_REQUESTS = Counter(
    "bifrost_local_requests_total",
    "Requests handled by local tiers",
    ["tier"],
)

REQUEST_LATENCY = Histogram(
    "bifrost_request_latency_ms",
    "Request latency in milliseconds",
    ["band", "tier"],
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000],
)

ACTIVE_MODE = Gauge(
    "bifrost_active_mode",
    "Current operating mode (1=active)",
    ["mode"],
)

LOCAL_PERCENTAGE = Gauge(
    "bifrost_local_percentage",
    "Percentage of requests handled locally",
)

UPTIME = Gauge(
    "bifrost_uptime_seconds",
    "Router uptime in seconds",
)

TIER_TOK_S = Gauge(
    "bifrost_tier_tok_s",
    "Last observed tokens per second for each tier",
    ["tier"],
)


# ---------------------------------------------------------------------------
# v98 -- Routing decision metrics (NPU live promotion)
# ---------------------------------------------------------------------------

bifrost_routing_decisions_total = Counter(
    "bifrost_routing_decisions_total",
    "Routing decisions by source/tier/agreement (v98)",
    ["source", "tier", "agreement"],
)

bifrost_npu_confidence = Histogram(
    "bifrost_npu_confidence",
    "NPU classifier confidence distribution (v98)",
    buckets=[0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95, 1.0],
)

bifrost_npu_latency_ms = Histogram(
    "bifrost_npu_latency_ms",
    "NPU classifier latency in milliseconds (v98)",
    buckets=[50, 100, 200, 500, 1000, 2000, 5000, 10000],
)

bifrost_routing_fallback_total = Counter(
    "bifrost_routing_fallback_total",
    "Routing fallback events by reason (v98)",
    ["reason"],
)


# ---------------------------------------------------------------------------
# In-memory event log (for /metrics/recent JSON endpoint)
# ---------------------------------------------------------------------------

@dataclass
class RoutingEvent:
    timestamp: float
    band: ComplexityBand
    tier: Tier
    latency_ms: float
    success: bool
    escalated: bool = False
    escalation_from: Tier | None = None
    tok_s: float = 0.0


class MetricsCollector:
    """Thread-safe collector that updates both in-memory and Prometheus metrics."""

    def __init__(self):
        self._lock = Lock()
        self._events: list[RoutingEvent] = []
        self._band_counts: dict[str, int] = defaultdict(int)
        self._tier_counts: dict[str, int] = defaultdict(int)
        self._escalation_count: int = 0
        self._total_requests: int = 0
        self._cloud_requests: int = 0
        self._start_time: float = time.time()

    def set_mode(self, mode: str):
        """Update the active mode gauge."""
        try:
            from config import OperatingMode
            all_modes = [om.value for om in OperatingMode]
        except Exception:
            all_modes = [
                "JARVIS", "WORKSHOP", "WORKSTATION", "REMOTE",
                "NOMAD", "CLOUD_PLUS", "CLOUD_ONLY", "WORKSHOP_OFFLINE", "JARVIS_OFFLINE"
            ]
        for m in all_modes:
            ACTIVE_MODE.labels(mode=m).set(0)
        ACTIVE_MODE.labels(mode=mode).set(1)

    def set_info(self, mode: str, profile: str, version: str = "1.0.0", commit: str = "unknown"):
        """Set router info metric."""
        ROUTER_INFO.info({
            "mode": mode,
            "profile": profile,
            "version": version,
            "commit": commit,
        })

    def record(self, event: RoutingEvent):
        with self._lock:
            self._events.append(event)
            self._total_requests += 1
            self._band_counts[event.band.value] += 1
            self._tier_counts[event.tier.value] += 1

            is_cloud = event.tier.value.startswith("3")
            if event.escalated:
                self._escalation_count += 1
            if is_cloud:
                self._cloud_requests += 1

            if len(self._events) > 1000:
                self._events = self._events[-500:]

            # Prometheus counters
            REQUESTS_TOTAL.labels(
                band=event.band.value,
                tier=event.tier.value,
                success=str(event.success).lower(),
            ).inc()

            BAND_TOTAL.labels(band=event.band.value).inc()
            TIER_TOTAL.labels(tier=event.tier.value).inc()

            if event.tok_s and event.tok_s > 0:
                TIER_TOK_S.labels(tier=event.tier.value).set(event.tok_s)

            if is_cloud:
                CLOUD_REQUESTS.labels(tier=event.tier.value).inc()
            else:
                LOCAL_REQUESTS.labels(tier=event.tier.value).inc()

            if event.escalated and event.escalation_from:
                ESCALATIONS_TOTAL.labels(
                    from_tier=event.escalation_from.value,
                    to_tier=event.tier.value,
                ).inc()

            REQUEST_LATENCY.labels(
                band=event.band.value,
                tier=event.tier.value,
            ).observe(event.latency_ms)

            local_pct = (
                ((self._total_requests - self._cloud_requests) / self._total_requests * 100)
                if self._total_requests > 0 else 100.0
            )
            LOCAL_PERCENTAGE.set(local_pct)
            UPTIME.set(time.time() - self._start_time)

    def summary(self) -> dict:
        with self._lock:
            uptime = time.time() - self._start_time
            local_pct = (
                ((self._total_requests - self._cloud_requests) / self._total_requests * 100)
                if self._total_requests > 0 else 100.0
            )
            return {
                "uptime_seconds": round(uptime, 1),
                "total_requests": self._total_requests,
                "band_distribution": dict(self._band_counts),
                "tier_distribution": dict(self._tier_counts),
                "escalation_count": self._escalation_count,
                "cloud_requests": self._cloud_requests,
                "local_percentage": round(local_pct, 1),
            }

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            events = self._events[-n:]
            return [
                {
                    "timestamp": e.timestamp,
                    "band": e.band.value,
                    "tier": e.tier.value,
                    "latency_ms": round(e.latency_ms, 1),
                    "success": e.success,
                    "escalated": e.escalated,
                }
                for e in events
            ]

    def prometheus_export(self) -> tuple[bytes, str]:
        """Generate Prometheus exposition format."""
        UPTIME.set(time.time() - self._start_time)
        return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# Singleton
metrics = MetricsCollector()
