"""
BIFROST Router — Review Prompts
================================
Templates for the cloud review pass in TWO_PASS strategy.
Cloud provider selected by complexity band:
  MODERATE  → Groq  (fast, free)
  COMPLEX   → Gemini (deep context)
  FRONTIER  → Claude (compliance, frontier reasoning)
"""

from config import ComplexityBand


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
