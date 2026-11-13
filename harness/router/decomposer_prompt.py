"""
BIFROST Decomposer Prompt
Phase 3b — Task Graph Engine

Usage:
    from decomposer_prompt import build_decomposer_prompt
    prompt = build_decomposer_prompt(
        task="Build a CLI tool that converts Markdown to styled HTML",
        current_mode="JARVIS",
        unavailable_tiers=[],
        graph_id="graph-abc123",
        warp_seed=None,
    )
"""

from __future__ import annotations


DECOMPOSER_SYSTEM = """\
You are the Decomposer for BIFROST, a distributed AI inference platform.
Your job is to analyze a complex task and produce a directed acyclic graph (DAG)
of subtasks that can be executed in parallel across multiple inference tiers.

You have two possible outputs:
  1. <direct_route/> — task should NOT be decomposed
  2. <decomposition>...</decomposition> — a valid DAG of subtasks

Output ONLY the XML. No preamble, no explanation, no markdown fences.\
"""


TIER_BLOCK = """\
## Available Tiers

TRIVIAL — fast (>50 tok/s)
  Model specialty: code generation, boilerplate, autocomplete, single expressions
  Context budget: 4K tokens per subtask
  Think mode: never (always <think>false</think>)

MODERATE — medium (15–50 tok/s)
  Model specialty: code generation, function-level reasoning, single-file logic
  Context budget: 8K tokens per subtask
  Think mode: never (always <think>false</think>)

COMPLEX — slow (4–15 tok/s)
  Model specialty: instruction following, multi-file reasoning, architecture decisions
  Context budget: 16K tokens per subtask
  Think mode: available — use <think>true</think> for subtasks requiring deep
  sequential reasoning (tradeoff analysis, algorithm design, interface contracts)

FRONTIER — cloud latency
  Model specialty: novel architecture, security audit, final coherence review,
  genuinely stuck debugging where local chain is exhausted
  Context budget: 64K tokens per subtask
  Think mode: N/A
  Cost: ~100x local tiers — use sparingly

Current mode: {current_mode}
Unavailable tiers (do not assign): {unavailable_tiers}\
"""


DIRECT_ROUTE_BLOCK = """\
## When to return <direct_route/>

Return <direct_route/> immediately if ANY of these are true:
  - Task can be fully resolved in a single inference call
  - Task is primarily a question, explanation, lookup, or retrieval (not generation)
  - Task requires deep sequential reasoning where each step depends entirely on
    the previous — parallelism adds no value
  - Task has fewer than 2 genuinely independent components
  - More than 30% of subtasks would require <think>true</think> — this signals
    a sequential reasoning problem disguised as a parallelizable task
  - All subtasks would be on the COMPLEX/FRONTIER tier — no fan-out benefit\
"""


SCHEMA_BLOCK = """\
## Output Schema

You must output valid XML conforming EXACTLY to this structure.
Study the example carefully — it shows both an independent subtask (s1)
and a dependent subtask (s2), and correct use of <think>.

--- EXAMPLE OUTPUT ---
<decomposition>
  <graph_id>graph-abc123</graph_id>
  <complexity>MODERATE</complexity>
  <rationale>Task has 2 independent generation components and 1 integration step that benefits from parallel execution.</rationale>
  <subtasks>
    <subtask id="s1">
      <description>Generate a Python function that parses a Markdown string and returns an AST of heading, paragraph, and code block nodes.</description>
      <complexity>MODERATE</complexity>
      <think>false</think>
      <acceptance_criteria>
        <criterion>Function accepts a string and returns a list of typed AST nodes.</criterion>
        <criterion>Handles ATX headings (# ## ###), fenced code blocks, and plain paragraphs.</criterion>
        <criterion>Returns empty list for empty input without raising.</criterion>
      </acceptance_criteria>
      <dependencies/>
    </subtask>
    <subtask id="s2">
      <description>Generate a Python function that accepts the AST from s1 and renders it to an HTML string with a wrapping <article> tag.</description>
      <complexity>MODERATE</complexity>
      <think>false</think>
      <acceptance_criteria>
        <criterion>Accepts the AST node list produced by s1.</criterion>
        <criterion>Headings render as <h1>–<h3>, code blocks as <pre><code>, paragraphs as <p>.</criterion>
        <criterion>Output is valid HTML5 fragment.</criterion>
      </acceptance_criteria>
      <dependencies>
        <dep>s1</dep>
      </dependencies>
    </subtask>
    <subtask id="s3">
      <description>Design the interface contract between the parser (s1) and renderer (s2): define the AST node TypedDict schemas and document any edge cases in the contract.</description>
      <complexity>COMPLEX</complexity>
      <think>true</think>
      <acceptance_criteria>
        <criterion>Defines TypedDict for each node type (Heading, Paragraph, CodeBlock) with all required fields.</criterion>
        <criterion>Documents at least 3 edge cases and how the contract handles them.</criterion>
      </acceptance_criteria>
      <dependencies/>
    </subtask>
  </subtasks>
  <assembly_instruction>
    Integrate s3's interface contract into s1 and s2's implementations. Replace any
    informal dict usage with the TypedDict definitions. Run the combined parser +
    renderer pipeline against the acceptance criteria for both subtasks. Deliver as
    a single Python module with both functions and the TypedDict definitions at the top.
  </assembly_instruction>
</decomposition>
--- END EXAMPLE ---\
"""


CONSTRAINTS_BLOCK = """\
## Constraints — NON-NEGOTIABLE

- Maximum 20 subtasks total
- No circular dependencies — if s2 depends on s1, s1 must NOT depend on s2
- Every subtask must have at least 1 acceptance criterion
- Acceptance criteria must be concrete and testable, not vague ("works correctly" is not valid)
- Subtasks must be genuinely independent — no implicit shared mutable state
- All dependencies must be listed explicitly in <dependencies>
- Graph-level <complexity> must be MODERATE or COMPLEX — TRIVIAL tasks are Direct Route
- <think>true</think> only on COMPLEX subtasks — TRIVIAL and MODERATE are always <think>false</think>
- Maximum 30% of subtasks may have <think>true</think>
- Do not assign subtasks to unavailable tiers
- <assembly_instruction> must be specific enough for an automated assembler to act on
  without human interpretation — "combine the outputs" is not valid\
"""


WARP_SEED_BLOCK = """\
## WARP Seed

A fast draft of this task was already generated. Treat it as a starting point,
not a replacement for proper decomposition. Decompose to improve, verify, and
complete it — not to redo it from scratch.

Assign the most relevant subtask a <warp_draft> child element containing the
seed output. That subtask's executor will use it as its initial partial output.

<warp_seed>
  <tier_used>{warp_tier}</tier_used>
  <output>{warp_output}</output>
</warp_seed>\
"""


TASK_BLOCK = """\
## Task

{task}\
"""


def build_decomposer_prompt(
    task: str,
    current_mode: str,
    unavailable_tiers: list[str],
    graph_id: str,
    warp_seed: dict | None = None,
) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) tuple ready for Ollama/OpenAI API.

    Args:
        task:              The original user task string.
        current_mode:      e.g. "JARVIS", "WORKSHOP", "CLOUD_ONLY"
        unavailable_tiers: List of tier names not available right now.
                           e.g. ["2.5-forge"] in WORKSHOP mode.
        graph_id:          UUID for this decomposition job.
        warp_seed:         Optional dict with keys "tier_used" and "output".

    Returns:
        (system_prompt, user_prompt)
    """
    unavailable_str = ", ".join(unavailable_tiers) if unavailable_tiers else "none"

    tier_section = TIER_BLOCK.format(
        current_mode=current_mode,
        unavailable_tiers=unavailable_str,
    )

    warp_section = ""
    if warp_seed:
        warp_section = (
            "\n\n"
            + WARP_SEED_BLOCK.format(
                warp_tier=warp_seed["tier_used"],
                warp_output=warp_seed["output"],
            )
        )

    # Inject graph_id into schema block example
    schema_section = SCHEMA_BLOCK.replace("graph-abc123", graph_id)

    user_prompt = "\n\n".join([
        tier_section,
        DIRECT_ROUTE_BLOCK,
        schema_section,
        CONSTRAINTS_BLOCK,
    ])

    if warp_section:
        user_prompt += warp_section

    user_prompt += "\n\n" + TASK_BLOCK.format(task=task)

    return DECOMPOSER_SYSTEM, user_prompt


# ── Quick test harness ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json, sys

    system, user = build_decomposer_prompt(
        task=(
            "Build a CLI tool that converts Markdown files to styled HTML with "
            "table of contents, syntax highlighting, and a dark mode toggle."
        ),
        current_mode="JARVIS",
        unavailable_tiers=[],
        graph_id="graph-test-001",
        warp_seed=None,
    )

    print("=" * 60)
    print("SYSTEM PROMPT")
    print("=" * 60)
    print(system)
    print()
    print("=" * 60)
    print("USER PROMPT")
    print("=" * 60)
    print(user)
    print()
    print(f"System tokens (approx): {len(system.split())}")
    print(f"User tokens (approx):   {len(user.split())}")
    print(f"Total tokens (approx):  {len((system + user).split())}")
