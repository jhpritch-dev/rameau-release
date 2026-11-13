"""
pr_review.py — BIFROST PR Review Pipeline (Block 5)
====================================================
GitHub webhook → dual-model consensus review → PR comment

Architecture:
  POST /webhook/github
      ↓
  Verify HMAC signature
      ↓
  Extract diff + PR metadata
  (large diffs >500 lines → also route to Forge 14b)
      ↓
  Parallel review:
    • T1A_HEARTH  — qwen2.5-coder:7b  on Hearth 5700XT :11434  (fast first pass)
    • T1A_OVERFLOW — qwen3.5:4b       on Hearth Vega8   :11436  (consensus check)
    • T1B         — qwen2.5-coder:14b on Forge           :11434  (large diffs only)
      ↓
  Aggregate verdict: PASS / CONCERNS / FAIL
      ↓
  POST comment to GitHub PR

Environment variables required (set Machine-level on Bifrost):
  GITHUB_TOKEN          — PAT with repo scope
  GITHUB_WEBHOOK_SECRET — matches GitHub webhook config
"""

import hashlib
import hmac
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from config import Tier, TIER_BACKENDS, settings

log = logging.getLogger("bifrost.pr_review")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN          = os.environ.get("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
LARGE_DIFF_THRESHOLD  = 500   # lines — route to Forge 14b above this
REVIEW_TIMEOUT        = 120   # seconds per reviewer


# ---------------------------------------------------------------------------
# Review tiers
# ---------------------------------------------------------------------------

REVIEW_TIERS = [Tier.T1A_HEARTH, Tier.T1A_OVERFLOW]
LARGE_DIFF_EXTRA_TIER = Tier.T1B


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

REVIEW_SYSTEM = """\
You are a senior engineer reviewing a GitHub pull request diff.

Analyze the diff carefully and respond with EXACTLY this format (no extra text):

VERDICT: <PASS|CONCERNS|FAIL>

SUMMARY:
<2-4 sentence summary of what the PR does>

ISSUES:
<bullet list of bugs, security issues, logic errors, missing tests, or style problems>
<write "None" if there are no issues>

SUGGESTIONS:
<bullet list of optional improvements>
<write "None" if there are no suggestions>

Rules:
- PASS   = correct, safe, no significant issues
- CONCERNS = works but has notable issues worth discussing
- FAIL   = bugs, security problems, or fundamentally broken logic
Be direct and specific. Reference line numbers or function names where relevant."""


def _build_review_messages(pr_title: str, pr_body: str, diff: str, tier: Tier) -> list[dict]:
    """Build message list for a single reviewer tier."""
    diff_preview = diff[:8000] if len(diff) > 8000 else diff
    truncated = " [diff truncated to 8000 chars]" if len(diff) > 8000 else ""

    user_content = (
        f"## PR: {pr_title}\n\n"
        f"**Description:** {pr_body or 'No description provided.'}\n\n"
        f"## Diff{truncated}\n```diff\n{diff_preview}\n```"
    )
    return [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user",   "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Single reviewer call (sync — runs in ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def _call_reviewer(tier: Tier, messages: list[dict]) -> dict:
    """
    Call one reviewer tier synchronously.
    Returns dict with tier, verdict, full_response, error.
    """
    backend  = TIER_BACKENDS[tier]
    base_url = backend.get("base_url", settings.ollama_base_url)
    model    = backend["model"]

    try:
        with httpx.Client(timeout=REVIEW_TIMEOUT) as client:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model":    model,
                    "messages": messages,
                    "stream":   False,
                    "temperature": 0.2,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            verdict = _extract_verdict(text)
            log.info(f"PR review [{tier.value}]: {verdict}")
            return {"tier": tier.value, "verdict": verdict, "response": text, "error": None}
    except Exception as e:
        log.warning(f"PR review [{tier.value}] failed: {e}")
        return {"tier": tier.value, "verdict": "UNKNOWN", "response": "", "error": str(e)}


def _extract_verdict(text: str) -> str:
    """Extract PASS / CONCERNS / FAIL from reviewer response."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v in ("PASS", "CONCERNS", "FAIL"):
                return v
    # Fallback: scan for keywords
    upper = text.upper()
    if "FAIL" in upper:
        return "FAIL"
    if "CONCERNS" in upper:
        return "CONCERNS"
    if "PASS" in upper:
        return "PASS"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Consensus aggregation
# ---------------------------------------------------------------------------

VERDICT_WEIGHT = {"FAIL": 3, "CONCERNS": 2, "PASS": 1, "UNKNOWN": 0}


def _aggregate_verdicts(results: list[dict]) -> str:
    """
    Consensus: any FAIL → FAIL, majority CONCERNS → CONCERNS, else PASS.
    """
    verdicts = [r["verdict"] for r in results if r["verdict"] != "UNKNOWN"]
    if not verdicts:
        return "UNKNOWN"
    if "FAIL" in verdicts:
        return "FAIL"
    concerns_count = verdicts.count("CONCERNS")
    pass_count     = verdicts.count("PASS")
    if concerns_count >= pass_count:
        return "CONCERNS"
    return "PASS"


# ---------------------------------------------------------------------------
# Run parallel review
# ---------------------------------------------------------------------------

def run_pr_review(
    pr_title: str,
    pr_body:  str,
    diff:     str,
) -> dict:
    """
    Run the full PR review pipeline.
    Returns {verdict, results, comment_body}.
    """
    diff_lines = diff.count("\n")
    tiers = list(REVIEW_TIERS)
    if diff_lines > LARGE_DIFF_THRESHOLD:
        tiers.append(LARGE_DIFF_EXTRA_TIER)
        log.info(f"Large diff ({diff_lines} lines) — adding Forge 14b reviewer")

    messages = _build_review_messages(pr_title, pr_body, diff, tiers[0])

    results = []
    with ThreadPoolExecutor(max_workers=len(tiers)) as executor:
        futures = {
            executor.submit(_call_reviewer, tier, messages): tier
            for tier in tiers
        }
        for future in as_completed(futures):
            results.append(future.result())

    verdict = _aggregate_verdicts(results)
    comment = _build_comment(verdict, results, diff_lines)

    return {
        "verdict":      verdict,
        "results":      results,
        "diff_lines":   diff_lines,
        "comment_body": comment,
    }


# ---------------------------------------------------------------------------
# Comment builder
# ---------------------------------------------------------------------------

VERDICT_EMOJI = {"PASS": "✅", "CONCERNS": "⚠️", "FAIL": "❌", "UNKNOWN": "❓"}
VERDICT_LABEL = {"PASS": "PASS", "CONCERNS": "CONCERNS — review recommended", "FAIL": "FAIL — do not merge", "UNKNOWN": "UNKNOWN"}


def _build_comment(verdict: str, results: list[dict], diff_lines: int) -> str:
    """Build the GitHub PR comment body."""
    emoji = VERDICT_EMOJI.get(verdict, "❓")
    label = VERDICT_LABEL.get(verdict, verdict)

    lines = [
        f"## {emoji} BIFROST Code Review — {label}",
        "",
        f"*{len(results)} reviewer(s) · {diff_lines} diff lines*",
        "",
    ]

    for r in results:
        if r["error"]:
            lines += [f"### `{r['tier']}` — ⚠️ reviewer error", f"```\n{r['error']}\n```", ""]
            continue

        tier_emoji = VERDICT_EMOJI.get(r["verdict"], "❓")
        lines.append(f"### `{r['tier']}` — {tier_emoji} {r['verdict']}")

        # Extract sections from response
        response = r["response"]
        in_section = None
        section_lines = []
        for line in response.splitlines():
            stripped = line.strip()
            if stripped.startswith("SUMMARY:"):
                if in_section and section_lines:
                    lines += _render_section(in_section, section_lines)
                in_section = "SUMMARY"
                section_lines = []
            elif stripped.startswith("ISSUES:"):
                if in_section and section_lines:
                    lines += _render_section(in_section, section_lines)
                in_section = "ISSUES"
                section_lines = []
            elif stripped.startswith("SUGGESTIONS:"):
                if in_section and section_lines:
                    lines += _render_section(in_section, section_lines)
                in_section = "SUGGESTIONS"
                section_lines = []
            elif stripped.startswith("VERDICT:"):
                continue
            elif in_section and stripped:
                section_lines.append(stripped)

        if in_section and section_lines:
            lines += _render_section(in_section, section_lines)
        lines.append("")

    lines += [
        "---",
        "*Reviewed by [BIFROST](https://github.com/jhpritch-dev/bifrost-platform) "
        "· local inference · privacy-first*",
    ]
    return "\n".join(lines)


def _render_section(name: str, content: list[str]) -> list[str]:
    out = [f"**{name.title()}:** {content[0]}" if len(content) == 1 else f"**{name.title()}:**"]
    if len(content) > 1:
        out += [f"- {l.lstrip('- •*').strip()}" for l in content if l.strip().lower() != "none"]
    return out


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> bool:
    """Post a comment to a GitHub PR. Returns True on success."""
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN not set — cannot post PR comment")
        return False

    url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json={"body": body}, headers=headers)
            resp.raise_for_status()
            log.info(f"PR comment posted to {repo_full_name}#{pr_number}")
            return True
    except Exception as e:
        log.error(f"Failed to post PR comment: {e}")
        return False


def fetch_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR from GitHub API."""
    if not GITHUB_TOKEN:
        log.error("GITHUB_TOKEN not set")
        return ""

    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        log.error(f"Failed to fetch PR diff: {e}")
        return ""


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_github_signature(payload: bytes, signature_header: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not GITHUB_WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature verification")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
