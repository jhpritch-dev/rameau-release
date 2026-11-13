"""
BIFROST Router â€” Slash Commands
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
from autopilot_graph import run_autopilot
from autopilot_graph import run_autopilot


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARBITER_URL = "http://localhost:8082"

STATUS_EMOJI = {
    "healthy": "ðŸŸ¢",
    "available": "ðŸŸ¡",
    "degraded": "ðŸŸ ",
    "offline": "ðŸ”´",
    "placeholder": "âšª",
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

    lines = ["## âš¡ BIFROST Status", ""]

    if arbiter_data:
        mode = arbiter_data.get("confirmed_mode", "UNKNOWN")
        candidate = arbiter_data.get("candidate_mode")
        mode_label = f"{mode} (stable)" if not candidate else (
            f"{mode} â†’ {candidate} pending"
        )
        lines.append(f"**Mode:** {mode_label}")
    else:
        lines.append(f"**Mode:** {settings.current_mode.value} (Router-local)")
        lines.append("âš ï¸ Arbiter unreachable â€” showing Router-local state only.")

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
            emoji = STATUS_EMOJI.get(status, "âšª")
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
            f"## ðŸ”„ Review Mode: {status_label}",
            "",
        ]

        if is_two_pass:
            lines.append(
                "Local drafts â†’ Cloud review for MODERATE+ requests. "
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
    if action in ("on", "off"):
        # 2026-10-13 audit: the default strategy is CONTROL state. POST /strategy changes it only
        # with the X-Bifrost-Control token (fail-closed); this chat path changed it with none.
        return (
            "`/review on|off` no longer changes the router from chat: the default strategy is "
            "control state. Use POST /strategy with the X-Bifrost-Control token. "
            "`/review status` still works."
        )
    return "Invalid argument. Use `/review [on|off|status]`."


# ---------------------------------------------------------------------------
# Command: /bifrost
# ---------------------------------------------------------------------------

async def cmd_bifrost(args: list[str], settings: Any) -> str:
    if not args:
        profile = settings.bifrost_profile
        details = PROFILE_DETAILS.get(profile, {"models": "Unknown", "vram": "?"})
        return (
            f"## ðŸ”§ Bifrost Profile: {profile.value}\n\n"
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

    # 2026-10-13 audit: chat no longer writes process-wide settings. bifrost_profile only labels
    # /status, the startup log and the Prometheus info gauge today, but writing it is still
    # not a chat message's to do.
    return ("`/bifrost <profile>` no longer changes the router from chat. "
            "`/bifrost` with no argument still shows the current profile.")


# ---------------------------------------------------------------------------
# Command: /mode
# ---------------------------------------------------------------------------

async def cmd_mode(client: httpx.AsyncClient) -> str:
    transitions = await fetch_arbiter(client, "/transitions")

    if transitions is None:
        return (
            "## ðŸ“Š Mode History\n\n"
            "âš ï¸ Arbiter unreachable â€” cannot retrieve transition history."
        )

    if not transitions:
        return (
            "## ðŸ“Š Mode History\n\n"
            "No mode transitions recorded since Arbiter started."
        )

    # Most recent transition tells us current mode
    latest = transitions[0]  # newest first from Arbiter
    current_mode = latest.get("to_mode", "UNKNOWN")

    # Time since last transition
    last_ts = latest.get("timestamp", 0)
    stable_for = format_uptime(time.time() - last_ts) if last_ts else "unknown"

    lines = [
        "## ðŸ“Š Mode History",
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
        f"## ðŸ’° Cloud Cost: {period.capitalize()}",
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
        f"## ðŸ”€ Cascade Table ({mode.value} mode)",
        "",
        "| Band | Cascade |",
        "|------|---------|",
    ]

    def format_cascade(tiers: list) -> str:
        names = [t.value if isinstance(t, Tier) else str(t) for t in tiers]
        return " â†’ ".join(names) if names else "none"

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
        "## ðŸ“– BIFROST Commands\n"
        "\n"
        "| Command | Description |\n"
        "|---------|-------------|\n"
        "| `/status` | System dashboard â€” mode, tiers, stats |\n"
        "| `/review [on\\|off]` | Toggle TWO_PASS review mode |\n"
        "| `/bifrost [light\\|dual\\|heavy]` | Switch GPU profile |\n"
        "| `/mode` | Mode transition history |\n"
        "| `/cost [today\\|week\\|month]` | Cloud API spend report |\n"
        "| `/cascade [band]` | Show routing cascade |\n"
        "| `/ap <task>` | Submit task to AUTOPILOT pipeline (decompose + execute + assemble) |\n"
        "| `/ap <task>` | Submit task to AUTOPILOT pipeline (decompose + execute + assemble) |\n"
        "| `/help` | This help message |"
    )



# ---------------------------------------------------------------------------
# Command: /ap  (alias: /autopilot)
# ---------------------------------------------------------------------------

def cmd_autopilot(args: list[str]) -> str:
    """
    Submit a task to the AUTOPILOT pipeline directly from Continue.dev.

    Usage:
      /ap <task description>
      /autopilot <task description>

    Example:
      /ap Write a Python async task queue with priority levels and Prometheus metrics

    The task is decomposed, fanned out across the fleet, and assembled.
    Returns the result with metadata (subtasks, tiers used, cost).
    """
    if not args:
        return (
            "## AUTOPILOT\n\n"
            "Usage: `/ap <task description>`\n\n"
            "Example: `/ap Write a Python CSV parser with error handling and type hints`\n\n"
            "AUTOPILOT decomposes complex tasks into parallel subtasks, "
            "executes them across the fleet with hierarchical escalation, "
            "and assembles the final result."
        )

    task = " ".join(args)
    import uuid as _uuid

    graph_id = str(_uuid.uuid4())[:8]

    try:
        result = run_autopilot(prompt=task, graph_id=graph_id)
    except Exception as e:
        return f"## AUTOPILOT Error\n\n```\n{str(e)[:400]}\n```"

    status      = result.get("status", "UNKNOWN")
    completed   = len(result.get("completed", {}))
    failed      = len(result.get("failed", {}))
    subtasks    = completed + failed
    cost        = result.get("cloud_cost_usd", 0.0)
    output      = result.get("assembled_output") or ""
    human_flag  = result.get("human_review_required", False)

    # Status line
    status_emoji = {
        "COMPLETE":           "âœ…",
        "FAILED_NEEDS_HUMAN": "âš ï¸",
        "FAILED":             "âŒ",
    }.get(status, "â³")

    meta_parts = [f"subtasks: {subtasks}"]
    if completed != subtasks:
        meta_parts.append(f"completed: {completed}")
    if failed:
        meta_parts.append(f"failed: {failed}")
    if cost > 0:
        meta_parts.append(f"cloud: ${cost:.4f}")
    if human_flag:
        meta_parts.append("âš ï¸ human review recommended")

    meta = " Â· ".join(meta_parts)

    lines = [
        f"## {status_emoji} AUTOPILOT â€” {status}",
        f"*{meta}*",
        "",
    ]

    if output:
        lines.append(output)
    elif status == "FAILED_NEEDS_HUMAN":
        failed_details = result.get("failed", {})
        lines.append("**Failed subtasks:**")
        for sid, err in failed_details.items():
            lines.append(f"- `{sid}`: {err}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command: /ap  (alias: /autopilot)
# ---------------------------------------------------------------------------

def cmd_autopilot(args: list[str]) -> str:
    """
    Submit a task to the AUTOPILOT pipeline directly from Continue.dev.

    Usage:
      /ap <task description>
      /autopilot <task description>

    Example:
      /ap Write a Python async task queue with priority levels and Prometheus metrics

    The task is decomposed, fanned out across the fleet, and assembled.
    Returns the result with metadata (subtasks, tiers used, cost).
    """
    if not args:
        return (
            "## AUTOPILOT\n\n"
            "Usage: `/ap <task description>`\n\n"
            "Example: `/ap Write a Python CSV parser with error handling and type hints`\n\n"
            "AUTOPILOT decomposes complex tasks into parallel subtasks, "
            "executes them across the fleet with hierarchical escalation, "
            "and assembles the final result."
        )

    task = " ".join(args)
    import uuid as _uuid

    graph_id = str(_uuid.uuid4())[:8]

    try:
        result = run_autopilot(prompt=task, graph_id=graph_id)
    except Exception as e:
        return f"## AUTOPILOT Error\n\n```\n{str(e)[:400]}\n```"

    status      = result.get("status", "UNKNOWN")
    completed   = len(result.get("completed", {}))
    failed      = len(result.get("failed", {}))
    subtasks    = completed + failed
    cost        = result.get("cloud_cost_usd", 0.0)
    output      = result.get("assembled_output") or ""
    human_flag  = result.get("human_review_required", False)

    # Status line
    status_emoji = {
        "COMPLETE":           "âœ…",
        "FAILED_NEEDS_HUMAN": "âš ï¸",
        "FAILED":             "âŒ",
    }.get(status, "â³")

    meta_parts = [f"subtasks: {subtasks}"]
    if completed != subtasks:
        meta_parts.append(f"completed: {completed}")
    if failed:
        meta_parts.append(f"failed: {failed}")
    if cost > 0:
        meta_parts.append(f"cloud: ${cost:.4f}")
    if human_flag:
        meta_parts.append("âš ï¸ human review recommended")

    meta = " Â· ".join(meta_parts)

    lines = [
        f"## {status_emoji} AUTOPILOT â€” {status}",
        f"*{meta}*",
        "",
    ]

    if output:
        lines.append(output)
    elif status == "FAILED_NEEDS_HUMAN":
        failed_details = result.get("failed", {})
        lines.append("**Failed subtasks:**")
        for sid, err in failed_details.items():
            lines.append(f"- `{sid}`: {err}")

    return "\n".join(lines)

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
    elif command in ("autopilot", "ap"):
        return cmd_autopilot(args)
    elif command == "forge":
        return "Forge not connected â€” available after Phase 2 setup."
    else:
        # Unrecognized â€” pass through to inference
        return None

