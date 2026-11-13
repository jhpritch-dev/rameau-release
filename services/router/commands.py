"""
BIFROST Router — Slash Commands
================================
Intercepts /command messages in chat completions and returns
formatted system responses instead of routing to inference.

Usage: import handle_command in main.py, call before classification.
"""

import time
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx

# Import from existing Router modules
from config import (
    BifrostProfile,
    ComplexityBand,
    CASCADE_TABLES,
    OperatingMode,
    RoutingStrategy,
    Tier,
    settings,
)
from metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARBITER_URL = "http://localhost:8082"

STATUS_EMOJI = {
    "healthy": "🟢",
    "available": "🟡",
    "degraded": "🟠",
    "offline": "🔴",
    "placeholder": "⚪",
}

PROFILE_DETAILS = {
    BifrostProfile.B_LIGHT: {
        "models": "- qwen2.5-coder:7b (Tier 1a-coder, autocomplete)",
        "vram": "~5GB / 16GB",
    },
    BifrostProfile.B_DUAL: {
        "models": (
            "- qwen2.5-coder:7b (Tier 1a-coder, autocomplete)\n"
            "- qwen2.5:7b-instruct (Tier 1a-instruct, reasoning)"
        ),
        "vram": "~10GB / 16GB",
    },
    BifrostProfile.B_HEAVY: {
        "models": "- qwen2.5-coder:14b (Tier 1a-coder, quality code generation)",
        "vram": "~12GB / 16GB",
    },
}

# Rough per-token cloud costs for savings estimation
CLOUD_COST_PER_1K_INPUT = 0.03
CLOUD_COST_PER_1K_OUTPUT = 0.06


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_command(raw: str) -> tuple[str, list[str]]:
    """Parse '/command arg1 arg2' into ('command', ['arg1', 'arg2'])."""
    parts = raw.strip().split()
    command = parts[0].lstrip("/").lower()
    args = parts[1:] if len(parts) > 1 else []
    return command, args


def format_uptime(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


async def fetch_arbiter(
    client: httpx.AsyncClient, path: str
) -> Optional[dict]:
    """Fetch from Arbiter, return parsed JSON or None on failure."""
    try:
        resp = await client.get(f"{ARBITER_URL}{path}", timeout=3.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def is_cloud_tier(tier_name: str) -> bool:
    """Check if a tier name represents a cloud provider."""
    return tier_name.startswith("3-")


# ---------------------------------------------------------------------------
# Command: /status
# ---------------------------------------------------------------------------

async def cmd_status(
    settings: Any,
    metrics: MetricsCollector,
    client: httpx.AsyncClient,
) -> str:
    arbiter_data = await fetch_arbiter(client, "/mode")

    lines = ["## ⚡ BIFROST Status", ""]

    if arbiter_data:
        mode = arbiter_data.get("confirmed_mode", "UNKNOWN")
        candidate = arbiter_data.get("candidate_mode")
        mode_label = f"{mode} (stable)" if not candidate else (
            f"{mode} → {candidate} pending"
        )
        lines.append(f"**Mode:** {mode_label}")
    else:
        lines.append(f"**Mode:** {settings.current_mode.value} (Router-local)")
        lines.append("⚠️ Arbiter unreachable — showing Router-local state only.")

    lines.append(f"**Strategy:** {settings.default_strategy.value}")
    lines.append(f"**Profile:** {settings.bifrost_profile.value}")
    lines.append("")

    # Tiers from Arbiter
    if arbiter_data and "tiers" in arbiter_data:
        lines.append("### Tiers")
        lines.append("| Tier | Status | Detail |")
        lines.append("|------|--------|--------|")
        for tier_id, info in arbiter_data["tiers"].items():
            status = info.get("status", "offline") if isinstance(info, dict) else "offline"
            detail = info.get("detail", "") if isinstance(info, dict) else ""
            emoji = STATUS_EMOJI.get(status, "⚪")
            lines.append(f"| {tier_id} | {emoji} {status} | {detail} |")
        lines.append("")

    # Session stats from metrics
    now = time.time()
    one_hour_ago = now - 3600
    recent = [
        e for e in metrics._events
        if e.timestamp >= one_hour_ago
    ]

    if recent:
        local_count = sum(1 for e in recent if not is_cloud_tier(e.tier))
        cloud_count = len(recent) - local_count
        total = len(recent)
        local_pct = int(local_count / total * 100) if total > 0 else 0
        cloud_pct = 100 - local_pct

        lines.append("### Session Stats (last 1h)")
        lines.append(
            f"Requests: {total} | Local: {local_count} ({local_pct}%)"
            f" | Cloud: {cloud_count} ({cloud_pct}%)"
        )
    else:
        lines.append("### Session Stats (last 1h)")
        lines.append("No requests recorded yet.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: /review
# ---------------------------------------------------------------------------

async def cmd_review(
    args: list[str],
    settings: Any,
    metrics: MetricsCollector,
) -> str:
    if not args or args[0].lower() == "status":
        # Show current state + real stats
        is_two_pass = settings.default_strategy == RoutingStrategy.TWO_PASS
        status_label = "ON (TWO_PASS)" if is_two_pass else "OFF (INTERACTIVE)"

        lines = [
            f"## 🔄 Review Mode: {status_label}",
            "",
        ]

        if is_two_pass:
            lines.append(
                "Local drafts → Cloud review for MODERATE+ requests. "
                "TRIVIAL requests still go direct to Tier 1a."
            )

        # Compute real stats from metrics
        two_pass_events = [
            e for e in metrics._events
            if getattr(e, "strategy", None) == "TWO_PASS"
        ]

        if two_pass_events:
            draft_times = [
                getattr(e, "draft_latency_ms", 0) for e in two_pass_events
                if getattr(e, "draft_latency_ms", None) is not None
            ]
            review_times = [
                getattr(e, "review_latency_ms", 0) for e in two_pass_events
                if getattr(e, "review_latency_ms", None) is not None
            ]

            lines.append("")
            lines.append("**Stats (this session):**")
            lines.append(f"- Drafts: {len(two_pass_events)}")
            if draft_times:
                avg_draft = sum(draft_times) / len(draft_times) / 1000
                lines.append(f"- Avg draft time: {avg_draft:.1f}s")
            if review_times:
                avg_review = sum(review_times) / len(review_times) / 1000
                lines.append(f"- Avg review time: {avg_review:.1f}s")
        else:
            lines.append("")
            lines.append("No TWO_PASS requests recorded yet.")

        return "\n".join(lines)

    action = args[0].lower()
    if action == "on":
        settings.default_strategy = RoutingStrategy.TWO_PASS
        return (
            "## 🔄 Review Mode: ON\n\n"
            "Strategy set to **TWO_PASS**. "
            "MODERATE+ requests will draft locally then review via cloud."
        )
    elif action == "off":
        settings.default_strategy = RoutingStrategy.INTERACTIVE
        return (
            "## 🔄 Review Mode: OFF\n\n"
            "Strategy set to **INTERACTIVE**. Single-pass routing resumed."
        )
    else:
        return "Invalid argument. Use `/review [on|off|status]`."


# ---------------------------------------------------------------------------
# Command: /bifrost
# ---------------------------------------------------------------------------

async def cmd_bifrost(args: list[str], settings: Any) -> str:
    if not args:
        profile = settings.bifrost_profile
        details = PROFILE_DETAILS.get(profile, {"models": "Unknown", "vram": "?"})
        return (
            f"## 🔧 Bifrost Profile: {profile.value}\n\n"
            f"**Loaded models:**\n{details['models']}\n\n"
            f"**VRAM usage:** {details['vram']}"
        )

    profile_map = {
        "light": BifrostProfile.B_LIGHT,
        "dual": BifrostProfile.B_DUAL,
        "heavy": BifrostProfile.B_HEAVY,
    }

    arg = args[0].lower()
    if arg not in profile_map:
        return "Invalid argument. Use `/bifrost [light|dual|heavy]`."

    new_profile = profile_map[arg]
    old_profile = settings.bifrost_profile
    settings.bifrost_profile = new_profile
    details = PROFILE_DETAILS.get(new_profile, {"models": "Unknown", "vram": "?"})

    return (
        f"## 🔧 Bifrost Profile: {old_profile.value} → {new_profile.value}\n\n"
        f"**Loaded models:**\n{details['models']}\n\n"
        f"**VRAM usage:** {details['vram']}"
    )


# ---------------------------------------------------------------------------
# Command: /mode
# ---------------------------------------------------------------------------

async def cmd_mode(client: httpx.AsyncClient) -> str:
    transitions = await fetch_arbiter(client, "/transitions")

    if transitions is None:
        return (
            "## 📊 Mode History\n\n"
            "⚠️ Arbiter unreachable — cannot retrieve transition history."
        )

    if not transitions:
        return (
            "## 📊 Mode History\n\n"
            "No mode transitions recorded since Arbiter started."
        )

    # Most recent transition tells us current mode
    latest = transitions[0]  # newest first from Arbiter
    current_mode = latest.get("to_mode", "UNKNOWN")

    # Time since last transition
    last_ts = latest.get("timestamp", 0)
    stable_for = format_uptime(time.time() - last_ts) if last_ts else "unknown"

    lines = [
        "## 📊 Mode History",
        "",
        f"**Current:** {current_mode} (stable for {stable_for})",
        "",
        "| Time | From | To | Debounce | Trigger |",
        "|------|------|----|----------|---------|",
    ]

    for t in transitions:
        ts = datetime.fromtimestamp(t.get("timestamp", 0)).strftime("%H:%M")
        from_m = t.get("from_mode", "?")
        to_m = t.get("to_mode", "?")
        debounce = f"{t.get('debounce_duration', 0):.1f}s"
        trigger = t.get("trigger", "")
        lines.append(f"| {ts} | {from_m} | {to_m} | {debounce} | {trigger} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: /cost
# ---------------------------------------------------------------------------

async def cmd_cost(args: list[str], metrics: MetricsCollector) -> str:
    period = args[0].lower() if args else "today"
    now = datetime.now()

    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        return "Invalid argument. Use `/cost [today|week|month]`."

    cutoff_ts = cutoff.timestamp()

    # Filter events in time window
    events_in_window = [
        e for e in metrics._events
        if e.timestamp >= cutoff_ts
    ]

    # Separate cloud vs local
    cloud_events = [e for e in events_in_window if is_cloud_tier(e.tier)]
    local_events = [e for e in events_in_window if not is_cloud_tier(e.tier)]

    total_cost = sum(getattr(e, "cloud_cost_usd", 0) or 0 for e in cloud_events)

    # Aggregate by provider
    provider_stats: dict[str, dict] = {}
    for e in cloud_events:
        provider = e.tier  # e.g. "3-groq", "3-claude", "3-gemini"
        if provider not in provider_stats:
            provider_stats[provider] = {
                "requests": 0,
                "cost": 0.0,
                "tokens_in": 0,
                "tokens_out": 0,
            }
        provider_stats[provider]["requests"] += 1
        provider_stats[provider]["cost"] += getattr(e, "cloud_cost_usd", 0) or 0
        provider_stats[provider]["tokens_in"] += getattr(e, "tokens_in", 0) or 0
        provider_stats[provider]["tokens_out"] += getattr(e, "tokens_out", 0) or 0

    lines = [
        f"## 💰 Cloud Cost: {period.capitalize()}",
        "",
        f"**Total:** ${total_cost:.2f}",
        f"**Requests:** {len(cloud_events)} cloud-routed",
        "",
    ]

    if provider_stats:
        lines.append("| Provider | Requests | Cost | Tokens (in/out) |")
        lines.append("|----------|----------|------|-----------------|")
        for provider, stats in sorted(provider_stats.items()):
            tok_in = f"{stats['tokens_in'] / 1000:.1f}K"
            tok_out = f"{stats['tokens_out'] / 1000:.1f}K"
            lines.append(
                f"| {provider} | {stats['requests']} "
                f"| ${stats['cost']:.2f} | {tok_in} / {tok_out} |"
            )
        lines.append("")

    # Estimate savings from local routing
    if local_events:
        local_tokens_in = sum(getattr(e, "tokens_in", 0) or 0 for e in local_events)
        local_tokens_out = sum(getattr(e, "tokens_out", 0) or 0 for e in local_events)
        estimated_savings = (
            (local_tokens_in / 1000) * CLOUD_COST_PER_1K_INPUT
            + (local_tokens_out / 1000) * CLOUD_COST_PER_1K_OUTPUT
        )
        lines.append(
            f"**Local savings:** {len(local_events)} requests handled locally "
            f"(~${estimated_savings:.2f} equivalent saved)"
        )
    else:
        lines.append("**Local savings:** No local requests in this period.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: /cascade
# ---------------------------------------------------------------------------

async def cmd_cascade(args: list[str], settings: Any) -> str:
    mode = settings.current_mode

    # Pull cascade from the Router's actual config
    cascades = CASCADE_TABLES.get(mode, {})

    if not cascades:
        return f"No cascade table configured for mode {mode.value}."

    lines = [
        f"## 🔀 Cascade Table ({mode.value} mode)",
        "",
        "| Band | Cascade |",
        "|------|---------|",
    ]

    def format_cascade(tiers: list) -> str:
        names = [t.value if isinstance(t, Tier) else str(t) for t in tiers]
        return " → ".join(names) if names else "none"

    if args:
        band_arg = args[0].upper()
        if band_arg not in ComplexityBand.__members__:
            return "Invalid argument. Use `/cascade [trivial|moderate|complex|frontier]`."
        band = ComplexityBand[band_arg]
        cascade = cascades.get(band, [])
        lines.append(f"| {band.value} | {format_cascade(cascade)} |")
    else:
        for band in ComplexityBand:
            cascade = cascades.get(band, [])
            lines.append(f"| {band.value} | {format_cascade(cascade)} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: /help
# ---------------------------------------------------------------------------

def cmd_help() -> str:
    return (
        "## 📖 BIFROST Commands\n"
        "\n"
        "| Command | Description |\n"
        "|---------|-------------|\n"
        "| `/status` | System dashboard — mode, tiers, stats |\n"
        "| `/review [on\\|off]` | Toggle TWO_PASS review mode |\n"
        "| `/bifrost [light\\|dual\\|heavy]` | Switch GPU profile |\n"
        "| `/mode` | Mode transition history |\n"
        "| `/cost [today\\|week\\|month]` | Cloud API spend report |\n"
        "| `/cascade [band]` | Show routing cascade |\n"
        "| `/help` | This help message |"
    )


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

async def handle_command(
    raw_message: str,
    settings: Any,
    metrics: MetricsCollector,
    http_client: httpx.AsyncClient,
) -> Optional[str]:
    """
    Parse and execute a slash command.
    Returns formatted markdown string if the message is a command.
    Returns None if the message is not a command (pass through to routing).
    """
    command, args = parse_command(raw_message)

    if command == "status":
        return await cmd_status(settings, metrics, http_client)
    elif command == "review":
        return await cmd_review(args, settings, metrics)
    elif command == "bifrost":
        return await cmd_bifrost(args, settings)
    elif command == "mode":
        return await cmd_mode(http_client)
    elif command == "cost":
        return await cmd_cost(args, metrics)
    elif command == "cascade":
        return await cmd_cascade(args, settings)
    elif command == "help":
        return cmd_help()
    elif command == "autopilot":
        return "AUTOPILOT not yet implemented — coming in Phase 3."
    elif command == "forge":
        return "Forge not connected — available after Phase 2 setup."
    else:
        # Unrecognized — pass through to inference
        return None
