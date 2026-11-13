"""
BIFROST Router — Review Prompts
================================
Templates for the cloud review pass in TWO_PASS strategy.
Cloud provider selected by complexity band:
  MODERATE  → Groq  (fast, free)
  COMPLEX   → Gemini (deep context)
  FRONTIER  → Claude (compliance, frontier reasoning)
"""

from config import ComplexityBand, Tier


# ---------------------------------------------------------------------------
# System prompts — tuned per provider
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_GROQ = """\
You are a fast code reviewer. You receive an original request and a draft \
from a local AI model.

Your job:
- Fix bugs, logic errors, security issues
- Improve readability and best practices
- Add missing error handling or edge cases
- Keep what works — don't rewrite unnecessarily
- If the draft is solid, tighten it up and return it

Be concise. Go straight to the improved solution."""


REVIEW_SYSTEM_GEMINI = """\
You are an expert code reviewer with deep technical knowledge. You receive \
an original request and a draft from a local AI model.

Your job:
- Fix all bugs, logic errors, and security issues
- Improve architecture, design patterns, and code organization
- Add comprehensive error handling, edge cases, and type annotations
- Consider performance implications and suggest optimizations
- Evaluate the overall approach — if the draft took a wrong direction, \
  explain why and provide the correct approach
- Keep what's already good

Respond with the improved code followed by a brief summary of changes."""


REVIEW_SYSTEM_CLAUDE = """\
You are a senior staff engineer performing a thorough code review. You \
receive an original request and a draft from a local AI model.

Your job:
- Ensure correctness, security, and robustness
- Verify compliance with best practices and industry standards
- Evaluate architectural decisions and suggest improvements
- Add comprehensive error handling, input validation, and edge cases
- Consider maintainability, testability, and documentation
- If the approach is fundamentally flawed, explain why and provide \
  the correct solution from scratch
- Keep what's already excellent

Respond with the improved code followed by a clear changelog of what \
you fixed and why."""


REVIEW_SYSTEMS = {
    "groq": REVIEW_SYSTEM_GROQ,
    "gemini": REVIEW_SYSTEM_GEMINI,
    "claude": REVIEW_SYSTEM_CLAUDE,
}


# ---------------------------------------------------------------------------
# Band → provider mapping
# ---------------------------------------------------------------------------

BAND_PROVIDER_MAP = {
    ComplexityBand.MODERATE: "groq",
    ComplexityBand.COMPLEX: "gemini",
    ComplexityBand.FRONTIER: "claude",
}


# ---------------------------------------------------------------------------
# LOCAL review lane + free-first T3 selection   (staged 2026-08-31)
#
# Before this, the second pass was cloud-only: BAND_PROVIDER_MAP returned groq/gemini/claude and
# nothing else, so TWO_PASS could not complete without leaving the fleet -- including under
# WORKSHOP_OFFLINE, which main.py never special-cases.
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_LOCAL = """\
You are reviewing a draft answer produced by another local model.
Return the IMPROVED answer only. Do not narrate the review.
Do not restate the draft where it was already correct.
If the draft is already correct, return it unchanged."""

# Registered by MUTATION because this block is inserted AFTER REVIEW_SYSTEMS is defined.
# Without this, build_review_prompt falls through to REVIEW_SYSTEM_GROQ and a LOCAL review runs
# on a cloud-shaped prompt -- and REVIEW_SYSTEM_LOCAL would be dead code.
REVIEW_SYSTEMS["local"] = REVIEW_SYSTEM_LOCAL

# T2.5 review is IGOR (John, 2026-08-31), and Igor is Tier.T2_5_SOLO ("2.5-solo",
# gpt-oss-igor-65k). Tier.T2_5 ("2.5") is qwen3-coder:30b -- using it here would review with the
# wrong model and label the result an Igor review.
LOCAL_REVIEW_MAP = {
    ComplexityBand.MODERATE: Tier.T2,
    ComplexityBand.COMPLEX: Tier.T2_5_SOLO,
    ComplexityBand.FRONTIER: Tier.T2_5_SOLO,
}

# Free-first T3. There is NO cost field anywhere in the fleet_config tier schema (all 39 entries
# checked), so "free" is derived from the :free model suffix. Only 3-lite-free carries "free" in
# its tier id, so matching on the tier name would find 1 of 5.
FREE_T3_BY_BAND = {
    ComplexityBand.MODERATE: [Tier.T3_LITE_FREE, Tier.T3_REASONING],
    ComplexityBand.COMPLEX: [Tier.T3_REASONING, Tier.T3_SYNTHESIS, Tier.T3_CODER_SUPER],
    ComplexityBand.FRONTIER: [Tier.T3_CODER_SUPER, Tier.T3_SYNTHESIS],
}


class NoFreeTierAvailable(RuntimeError):
    """Raised instead of silently falling back to a paid provider."""


def _free_target_for(tier):
    """Return (provider, model) iff this tier CURRENTLY serves a :free model, else None.

    Re-derived per call from live fleet_config (inheriting its mtime hot-reload) rather than
    baked: OpenRouter moves models off the free pool, and a stale list quietly becomes a bill.
    """
    from config import _load_fleet_config
    want = getattr(tier, "value", tier)
    for entry in (_load_fleet_config() or {}).get("tiers", []):
        if entry.get("tier") == want:
            model = str(entry.get("active_model", ""))
            if not model.endswith(":free"):
                return None
            return (entry.get("provider") or "openrouter"), model
    return None


def select_free_t3(band: ComplexityBand):
    """Return (provider, model) for a FREE cloud tier. Never falls back to paid silently.

    The model is ALWAYS returned explicitly. openai_compat does `target_model = model or
    default_model`, and openrouter's default_model is a PAID Claude -- so returning a free
    provider without its model would bill a paid call while claiming to be free.
    """
    from config import settings
    for tier in FREE_T3_BY_BAND.get(band, [Tier.T3_LITE_FREE]):
        target = _free_target_for(tier)
        if target:
            provider, model = target
            assert model and model.endswith(":free"), "free selection must carry a :free model"
            return provider, model
    if getattr(settings, "allow_paid_escalation", False):
        return BAND_PROVIDER_MAP.get(band, "groq"), None    # paid: provider default is correct
    raise NoFreeTierAvailable(
        "no :free T3 tier available for band %s and allow_paid_escalation is false" % band
    )


def get_review_target(band: ComplexityBand, mode=None):
    """Return (kind, target, model).

    kind == "local"  -> target is a Tier value, model is None
    kind == "cloud"  -> target is a provider name, model may be an explicit :free model
    """
    from config import OperatingMode, settings
    mode = mode if mode is not None else settings.current_mode
    offline = mode in (OperatingMode.WORKSHOP_OFFLINE,)
    if offline or getattr(settings, "local_review_preferred", False):
        return "local", LOCAL_REVIEW_MAP.get(band, Tier.T2).value, None
    provider, model = select_free_t3(band)
    return "cloud", provider, model


def get_review_provider(band: ComplexityBand) -> str:
    """Select cloud review provider based on complexity band."""
    return BAND_PROVIDER_MAP.get(band, "groq")


# ---------------------------------------------------------------------------
# Build review prompt
# ---------------------------------------------------------------------------

def build_review_prompt(
    original_messages: list[dict],
    draft_response: str,
    provider: str,
    task_type: str = "code",
) -> list[dict]:
    """
    Build the message array for the cloud review pass.
    """
    user_prompt = ""
    for msg in reversed(original_messages):
        if msg.get("role") == "user":
            user_prompt = msg.get("content", "")
            break

    system = REVIEW_SYSTEMS.get(provider, REVIEW_SYSTEM_GROQ)

    review_content = f"""## Original Request
{user_prompt}

## Draft Response (from local model)
{draft_response}

## Your Task
Review and improve the draft above. Fix any issues and return the improved version."""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": review_content},
    ]


# ---------------------------------------------------------------------------
# Task type detection
# ---------------------------------------------------------------------------

def detect_task_type(prompt: str) -> str:
    """Heuristic to detect coding task type."""
    prompt_lower = prompt.lower()
    if any(w in prompt_lower for w in ["debug", "fix", "error", "bug", "broken", "failing"]):
        return "debug"
    if any(w in prompt_lower for w in ["refactor", "clean up", "improve", "restructure"]):
        return "refactor"
    if any(w in prompt_lower for w in ["explain", "what does", "how does", "why does"]):
        return "explain"
    return "code"
