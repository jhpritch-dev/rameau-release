import re
"""
router_graph.py -- BIFROST RouterGraph (Phase 3a)
INTERACTIVE strategy only. No checkpointing -- in-memory, short-lived.
"""

import asyncio
import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from backends.anthropic import anthropic_chat_completion
from backends.ollama import ollama_chat_completion
from bifrost_message import BifrostMessage, RouterRequest
import os
from config import Tier, settings
import time as _time
from telemetry import InferenceEvent, usage_split, write_event as _write_event

# Load API keys from environment (mirrors main.py startup)
if not settings.anthropic_api_key:
    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
if not settings.gemini_api_key:
    settings.gemini_api_key = os.environ.get("GEMINI_API_KEY")
if not settings.groq_api_key:
    settings.groq_api_key = os.environ.get("GROQ_API_KEY")


# ---------------------------------------------------------------------------
# Tier mapping -- assigned_tier string -> Tier enum + backend selector
# ---------------------------------------------------------------------------

_TIER_MAP: dict[str, Tier] = {
    "1a":          Tier.T1A_CODER,
    "1a-hearth":   Tier.T1A_HEARTH,
    "1a-overflow": Tier.T1A_OVERFLOW,
    "1b":          Tier.T1B,
    "2":           Tier.T2,
    "2.5":         Tier.T2_5,
    "3-claude":    Tier.T3_CLAUDE,
    "3-gemini":    Tier.T3_GEMINI,
    "3-groq":      Tier.T3_FAST,
}

_CLOUD_TIERS = {Tier.T3_CLAUDE, Tier.T3_GEMINI, Tier.T3_FAST}

_T1A_TIERS = {Tier.T1A_CODER, Tier.T1A_HEARTH, Tier.T1A_OVERFLOW, Tier.T1A_INSTRUCT}


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n", "", text)
    text = re.sub(r"\r?\n```$", "", text)
    return text.strip()


# Cost per token (blended input+output estimate)
_CLOUD_COST_PER_TOKEN = {
    Tier.T3_CLAUDE: 3.0 / 1_000_000,   # Sonnet 4.6 blended
    Tier.T3_GEMINI: 0.1 / 1_000_000,
    Tier.T3_FAST:   0.1 / 1_000_000,
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class RouterState(TypedDict):
    message:          BifrostMessage
    band:             str
    confidence:       float
    assigned_tier:    str
    response:         str
    tokens_used:      int
    cloud_cost_usd:   float
    strategy:         str
    escalation_count: int
    error:            str | None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def classify_node(state: RouterState) -> dict:
    payload = state["message"].payload
    prompt = payload.prompt
    complexity_hint = payload.complexity_hint

    if complexity_hint == "frontier":
        return {"band": "FRONTIER", "confidence": 1.0}

    tokens = len(prompt.split())
    complex_keywords  = ("architect", "design", "refactor", "security")
    moderate_keywords = ("implement", "function", "fix", "debug")

    if tokens > 500 or any(kw in prompt.lower() for kw in complex_keywords):
        return {"band": "COMPLEX",  "confidence": 0.85}
    if any(kw in prompt.lower() for kw in moderate_keywords):
        return {"band": "MODERATE", "confidence": 0.75}
    return {"band": "TRIVIAL", "confidence": 0.9}


def assign_tier_node(state: RouterState) -> dict:
    mapping = {
        "TRIVIAL":  "1a",
        "MODERATE": "1b",
        "COMPLEX":  "2.5",
        "FRONTIER": "3-claude",
    }
    return {"assigned_tier": mapping.get(state["band"], "1a")}


def execute_node(state: RouterState) -> dict:
    """Route to correct backend and execute. Wraps async in sync for LangGraph."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_execute_async(state))


async def _execute_async(state: RouterState) -> dict:
    payload = state["message"].payload
    assigned = state["assigned_tier"]
    tier = _TIER_MAP.get(assigned, Tier.T1A_CODER)

    # Build messages -- use payload.messages if present, else wrap prompt
    messages = list(payload.messages) if payload.messages else []
    if not messages:
        messages = [{"role": "user", "content": payload.prompt}]

    _t0 = _time.perf_counter()
    try:
        band = state.get('band', 'TRIVIAL')
        # Capability-aware think routing. `think` is a top-level Ollama field
        # (not nested in options). Only set it for thinking-capable models, and
        # default to False — preserve the legacy T1B COMPLEX escalation as the
        # one explicit reasoning case.
        from config import get_tier_capabilities
        _caps = get_tier_capabilities(tier)
        _think: bool | None = None
        if _caps.get("thinking", False):
            _think = False
            if tier == Tier.T1B and band == 'COMPLEX':
                _think = True

        if tier in _CLOUD_TIERS:
            result = await anthropic_chat_completion(
                messages=messages,
                stream=False,
                max_tokens=4096,
            )
        else:
            result = await ollama_chat_completion(
                messages=messages,
                tier=tier,
                stream=False,
                think=_think,
            )

        response_text = result["choices"][0]["message"]["content"]
        if tier in _T1A_TIERS:
            response_text = _strip_fences(response_text)
        usage = result.get("usage", {})
        tokens_used = usage.get("total_tokens", 0)
        _rg_tok_in, _rg_tok_out = usage_split(usage)   # CEREMONY-89

        cost = 0.0
        if tier in _CLOUD_COST_PER_TOKEN:
            cost = tokens_used * _CLOUD_COST_PER_TOKEN[tier]

        _latency_ms = (_time.perf_counter() - _t0) * 1000
        await _write_event(InferenceEvent(
            strategy=state.get("strategy", "INTERACTIVE"),
            complexity_band=state.get("band", ""),
            tier_used=assigned,
            tier_history=[assigned],
            model=tier.value,
            tokens_input=_rg_tok_in,
            tokens_output=_rg_tok_out,
            tokens_total=tokens_used,
            latency_ms=_latency_ms,
            cloud_cost_usd=cost,
            status="PASS",
        ))
        return {
            "response":       response_text,
            "tokens_used":    tokens_used,
            "cloud_cost_usd": cost,
            "error":          None,
        }

    except Exception as e:
        return {
            "response":       "",
            "tokens_used":    0,
            "cloud_cost_usd": 0.0,
            "error":          f"execute_node [{assigned}]: {str(e)[:200]}",
        }


def error_node(state: RouterState) -> dict:
    return {"error": state.get("error", "unknown error")}


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def route_after_assign(state: RouterState) -> str:
    return "error" if state.get("error") else "execute"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

builder = StateGraph(RouterState)
builder.add_node("classify",    classify_node)
builder.add_node("assign_tier", assign_tier_node)
builder.add_node("execute",     execute_node)
builder.add_node("error",       error_node)

builder.add_edge(START, "classify")
builder.add_edge("classify", "assign_tier")
builder.add_conditional_edges(
    "assign_tier",
    route_after_assign,
    {"execute": "execute", "error": "error"},
)
builder.add_edge("execute", END)
builder.add_edge("error",   END)

router_graph = builder.compile()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_cases = [
        ("What is 2 + 2",                           None,       "TRIVIAL",  "1a"),
        ("Implement a binary search function",       None,       "MODERATE", "1b"),
        ("Design a distributed consensus protocol", None,       "COMPLEX",  "2.5"),
        ("Reply with one word: hello",              "frontier", "FRONTIER", "3-claude"),
    ]

    for prompt, hint, expected_band, expected_tier in test_cases:
        msg = BifrostMessage(
            trace_id=str(uuid.uuid4()),
            source="test",
            payload=RouterRequest(
                type="router_request",
                prompt=prompt,
                strategy="INTERACTIVE",
                messages=[],
                complexity_hint=hint,
            ),
        )
        initial_state: RouterState = {
            "message":          msg,
            "band":             "",
            "confidence":       0.0,
            "assigned_tier":    "",
            "response":         "",
            "tokens_used":      0,
            "cloud_cost_usd":   0.0,
            "strategy":         "INTERACTIVE",
            "escalation_count": 0,
            "error":            None,
        }

        print(f"\nPrompt:   '{prompt[:50]}'")
        print(f"Hint:     {hint or 'none'}")
        result = router_graph.invoke(initial_state)

        band_ok = "OK" if result["band"] == expected_band else f"FAIL(got {result['band']})"
        tier_ok = "OK" if result["assigned_tier"] == expected_tier else f"FAIL(got {result['assigned_tier']})"
        print(f"Band:     {result['band']} [{band_ok}]")
        print(f"Tier:     {result['assigned_tier']} [{tier_ok}]")
        print(f"Tokens:   {result['tokens_used']}")
        print(f"Cost:     ${result['cloud_cost_usd']:.6f}")
        print(f"Error:    {result['error']}")
        if result["response"]:
            print(f"Response: {result['response'][:80]}")


