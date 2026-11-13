"""
BIFROST Complexity Router — Configuration
==========================================
Mode-aware routing configuration. The Router adapts its cascade
logic based on the current operating mode (from Broadcaster) and
available hardware.
"""

from enum import Enum
from pydantic import BaseModel
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComplexityBand(str, Enum):
    TRIVIAL = "TRIVIAL"
    MODERATE = "MODERATE"
    COMPLEX = "COMPLEX"
    FRONTIER = "FRONTIER"


class OperatingMode(str, Enum):
    JARVIS = "JARVIS"
    WORKSHOP = "WORKSHOP"
    WORKSTATION = "WORKSTATION"
    REMOTE = "REMOTE"
    NOMAD = "NOMAD"
    CLOUD_PLUS = "CLOUD_PLUS"
    CLOUD_ONLY = "CLOUD_ONLY"
    WORKSHOP_OFFLINE = "WORKSHOP_OFFLINE"


class Tier(str, Enum):
    T1A_CODER = "1a-coder"
    T1A_HEARTH = "1a-hearth"
    T1A_INSTRUCT = "1a-instruct"
    T1B = "1b"
    T2 = "2"
    T2_5 = "2.5"
    T3_CLAUDE = "3-claude"
    T3_GEMINI = "3-gemini"
    T3_FAST = "3-fast"


class BifrostProfile(str, Enum):
    B_LIGHT = "B-Light"
    B_DUAL = "B-Dual"
    B_HEAVY = "B-Heavy"


class RoutingStrategy(str, Enum):
    INTERACTIVE = "INTERACTIVE"
    TWO_PASS = "TWO_PASS"
    AUTOPILOT = "AUTOPILOT"


# ---------------------------------------------------------------------------
# Tier → Backend mapping
# ---------------------------------------------------------------------------

TIER_BACKENDS = {
    Tier.T1A_CODER: {
        "type": "ollama",
        "model": "qwen2.5-coder:7b",
        "base_url": "http://localhost:11434",
    },
    Tier.T1A_HEARTH: {
        "type": "ollama",
        "model": "qwen2.5-coder:7b-ctx4k",
        "base_url": "http://192.168.x.4:11434",
    },
    Tier.T1A_INSTRUCT: {
        "type": "ollama",
        "model": "qwen2.5:7b-instruct",
        "base_url": "http://localhost:11434",
    },
    Tier.T1B: {
        "type": "ollama",
        "model": "qwen2.5-coder:14b",
        "base_url": "http://localhost:11434",  # Bifrost for now; Forge in JARVIS
    },
    Tier.T2: {
        "type": "ollama",
        "model": "qwen2.5-coder:32b",
        "base_url": "http://localhost:11434",  # Forge when available
    },
    Tier.T2_5: {
        "type": "ollama",
        "model": "qwen2.5:72b",
        "base_url": "http://localhost:11434",  # Forge only
    },
    Tier.T3_CLAUDE: {
        "type": "anthropic",
        "model": "claude-sonnet-4-5-20250929",
    },
    Tier.T3_GEMINI: {
        "type": "openai_compat",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
    },
    Tier.T3_FAST: {
        "type": "openai_compat",
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
    },
}


# ---------------------------------------------------------------------------
# Mode → Cascade routing tables
# ---------------------------------------------------------------------------
# Each band maps to an ordered list of tiers to try.
# The Router walks the list until one succeeds or all fail.

CASCADE_TABLES = {
    OperatingMode.JARVIS: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1A_HEARTH, Tier.T1A_CODER, Tier.T1B, Tier.T2],
        ComplexityBand.COMPLEX:  [Tier.T2, Tier.T2_5, Tier.T3_CLAUDE],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE],
    },
    OperatingMode.WORKSHOP: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1A_HEARTH, Tier.T1A_CODER, Tier.T1B, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T3_GEMINI],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE],
    },
    OperatingMode.WORKSHOP_OFFLINE: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH, Tier.T1A_CODER],
        ComplexityBand.MODERATE: [Tier.T1A_HEARTH, Tier.T1A_CODER],
        ComplexityBand.COMPLEX:  [Tier.T1A_CODER],
        ComplexityBand.FRONTIER: [Tier.T1A_CODER],
    },
    OperatingMode.CLOUD_PLUS: {
        ComplexityBand.TRIVIAL:  [Tier.T1A_HEARTH],
        ComplexityBand.MODERATE: [Tier.T1A_HEARTH, Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T3_GEMINI],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE],
    },
    OperatingMode.CLOUD_ONLY: {
        ComplexityBand.TRIVIAL:  [Tier.T3_FAST],
        ComplexityBand.MODERATE: [Tier.T3_FAST],
        ComplexityBand.COMPLEX:  [Tier.T3_GEMINI],
        ComplexityBand.FRONTIER: [Tier.T3_CLAUDE],
    },
}

# Modes not explicitly listed fall back to WORKSHOP cascades
DEFAULT_CASCADE_MODE = OperatingMode.WORKSHOP


# ---------------------------------------------------------------------------
# Classifier thresholds (rule-based, Phase 1)
# ---------------------------------------------------------------------------

class ClassifierConfig(BaseModel):
    """Tunable thresholds for the rule-based complexity classifier."""

    # Token count thresholds
    trivial_max_tokens: int = 200
    moderate_max_tokens: int = 1500
    complex_max_tokens: int = 8000
    # Above complex_max_tokens → FRONTIER

    # File reference thresholds
    multi_file_threshold: int = 3  # ≥3 files referenced → bump up one band

    # Keyword weights (additive score)
    trivial_keywords: list[str] = [
        "autocomplete", "complete", "fill", "snippet", "boilerplate",
        "getter", "setter", "import", "require",
    ]
    moderate_keywords: list[str] = [
        "implement", "function", "method", "class", "test", "fix",
        "bug", "error", "docstring", "type", "interface", "endpoint",
        "handler", "validate", "parse", "decorator", "retry", "http",
        "backoff", "logging", "config", "serializ", "deserializ",
        "convert", "transform", "filter", "sort", "search", "crud",
        "api", "request", "response", "client", "server", "model",
        "util", "helper", "wrapper", "abstract", "generic", "template",
    ]
    complex_keywords: list[str] = [
        "refactor", "redesign", "migrate", "multi-file", "architecture",
        "system", "pipeline", "workflow", "integration", "database",
        "schema", "performance", "optimize", "concurrent", "async",
        "middleware", "algorithm", "redis", "cache", "queue",
        "authentication", "authorization", "oauth", "saml", "jwt",
        "websocket", "streaming", "batch", "scheduler", "cron",
        "rate limit", "throttle", "session", "federation",
    ]
    frontier_keywords: list[str] = [
        "architect", "design system", "tradeoff", "security audit",
        "threat model", "distributed", "consensus", "novel",
        "from scratch", "entire", "codebase", "review all",
        "byzantine", "fault tolerance", "formal proof", "correctness",
        "compiler", "language design", "type system", "kernel",
    ]


# ---------------------------------------------------------------------------
# Router settings
# ---------------------------------------------------------------------------

class RouterSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    current_mode: OperatingMode = OperatingMode.WORKSHOP
    bifrost_profile: BifrostProfile = BifrostProfile.B_DUAL
    default_strategy: RoutingStrategy = RoutingStrategy.INTERACTIVE

    # Broadcaster endpoint (for dynamic mode updates)
    broadcaster_url: Optional[str] = "http://192.168.x.4:8090"

    # Anthropic API (Tier 3-Claude — future)
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-5-20250929"

    # Gemini API (Tier 3-Gemini — default cloud)
    gemini_api_key: Optional[str] = None

    # Groq API (Tier 3-Fast — free fast fallback)
    groq_api_key: Optional[str] = None

    # Ollama
    ollama_base_url: str = "http://localhost:11434"

    # Timeouts (seconds)
    ollama_timeout: int = 120
    anthropic_timeout: int = 60
    gemini_timeout: int = 60
    groq_timeout: int = 30

    # Two-pass strategy settings
    two_pass_local_timeout: int = 30                # max seconds for local draft
    two_pass_min_band: str = "COMPLEX"              # minimum band to trigger two-pass (MODERATE stays local-only)

    # Confidence gate — skip cloud review if local draft passes checks
    confidence_gate_enabled: bool = True
    confidence_min_response_ratio: float = 0.3      # draft tokens / prompt tokens — below this = suspiciously short
    confidence_min_tokens: int = 20                  # absolute minimum draft length
    confidence_truncation_penalty: bool = True       # flag drafts that end mid-code-block
    confidence_hedging_penalty: bool = True          # flag drafts with hedging language

    # Logging
    log_routing_decisions: bool = True


# Singleton — loaded from env / config file at startup
settings = RouterSettings()
classifier_config = ClassifierConfig()
