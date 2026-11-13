"""
BIFROST Complexity Router — Rule-Based Classifier
===================================================
Phase 1 heuristic classifier. Scores incoming requests into
TRIVIAL / MODERATE / COMPLEX / FRONTIER bands.

Phase 2+ replaces this with a learned classifier on Forge NPU.
The rule-based version remains as the WORKSHOP-mode fallback
(Forge offline → no NPU → heuristic).
"""

import re
from dataclasses import dataclass

from config import ComplexityBand, classifier_config


@dataclass
class ClassificationResult:
    band: ComplexityBand
    confidence: float           # 0.0–1.0
    reasoning: str              # Human-readable explanation
    scores: dict[str, float]    # Per-band scores for debugging
    hint_override: bool = False # True if X-Complexity-Hint was used


def classify(
    prompt: str,
    file_count: int = 0,
    hint: str | None = None,
) -> ClassificationResult:
    """
    Classify a request into a complexity band.

    Args:
        prompt: The user's message / code prompt.
        file_count: Number of files referenced (from request metadata).
        hint: Optional client hint (X-Complexity-Hint header).

    Returns:
        ClassificationResult with band, confidence, and reasoning.
    """
    cfg = classifier_config

    # ------------------------------------------------------------------
    # 1. Client hint override (trust the caller if they say so)
    # ------------------------------------------------------------------
    if hint and hint.lower() in ("trivial", "moderate", "complex", "frontier"):
        band = ComplexityBand(hint.upper())
        return ClassificationResult(
            band=band,
            confidence=0.9,
            reasoning=f"Client hint override: X-Complexity-Hint={hint}",
            scores={},
            hint_override=True,
        )

    # ------------------------------------------------------------------
    # 2. Feature extraction
    # ------------------------------------------------------------------
    prompt_lower = prompt.lower()
    token_estimate = len(prompt.split())  # Rough whitespace tokenizer
    reasons = []

    # ------------------------------------------------------------------
    # 3. Keyword scoring
    # ------------------------------------------------------------------
    scores = {
        "TRIVIAL": 0.0,
        "MODERATE": 0.0,
        "COMPLEX": 0.0,
        "FRONTIER": 0.0,
    }

    for kw in cfg.trivial_keywords:
        if kw in prompt_lower:
            scores["TRIVIAL"] += 1.0

    for kw in cfg.moderate_keywords:
        if kw in prompt_lower:
            scores["MODERATE"] += 1.5  # Strong signal — these words mean real work

    for kw in cfg.complex_keywords:
        if kw in prompt_lower:
            scores["COMPLEX"] += 2.0  # Weighted higher — these are strong signals

    for kw in cfg.frontier_keywords:
        if kw in prompt_lower:
            scores["FRONTIER"] += 2.5  # Strongest signal

    # ------------------------------------------------------------------
    # 4. Token count signal
    # ------------------------------------------------------------------
    if token_estimate <= cfg.trivial_max_tokens:
        scores["TRIVIAL"] += 1.5
        reasons.append(f"short prompt ({token_estimate} tokens)")
    elif token_estimate <= cfg.moderate_max_tokens:
        scores["MODERATE"] += 1.5
        reasons.append(f"medium prompt ({token_estimate} tokens)")
    elif token_estimate <= cfg.complex_max_tokens:
        scores["COMPLEX"] += 1.5
        reasons.append(f"long prompt ({token_estimate} tokens)")
    else:
        scores["FRONTIER"] += 2.0
        reasons.append(f"very long prompt ({token_estimate} tokens)")

    # ------------------------------------------------------------------
    # 5. File count signal
    # ------------------------------------------------------------------
    if file_count >= cfg.multi_file_threshold:
        scores["COMPLEX"] += 2.0
        scores["FRONTIER"] += 1.0
        reasons.append(f"multi-file ({file_count} files)")
    elif file_count > 1:
        scores["MODERATE"] += 1.0
        reasons.append(f"{file_count} files referenced")

    # ------------------------------------------------------------------
    # 6. Structural signals
    # ------------------------------------------------------------------

    # Code blocks in prompt suggest providing context → higher complexity
    code_blocks = len(re.findall(r"```", prompt))
    if code_blocks >= 4:
        scores["COMPLEX"] += 1.5
        reasons.append(f"{code_blocks // 2}+ code blocks")
    elif code_blocks >= 2:
        scores["MODERATE"] += 1.0

    # Question marks — simple questions tend to be lower complexity
    questions = prompt.count("?")
    if questions == 0 and token_estimate < 50:
        # Very short, no question — likely autocomplete/fill
        # BUT only if no higher-band keywords triggered
        has_higher_keywords = (
            scores["MODERATE"] > 0 or scores["COMPLEX"] > 0 or scores["FRONTIER"] > 0
        )
        if not has_higher_keywords:
            scores["TRIVIAL"] += 1.5
            reasons.append("no question, very short, no keywords → likely autocomplete")

    # Multi-step indicators
    step_patterns = [
        r"(?:first|then|next|after that|finally|step \d)",
        r"(?:1\.|2\.|3\.)",
        r"(?:phase|stage|part \d)",
    ]
    for pattern in step_patterns:
        if re.search(pattern, prompt_lower):
            scores["COMPLEX"] += 1.0
            reasons.append("multi-step language detected")
            break

    # ------------------------------------------------------------------
    # 7. Determine band from scores
    # ------------------------------------------------------------------
    # On ties, prefer the HIGHER complexity band when keyword evidence
    # supports it. Rationale: under-routing costs quality (bad output),
    # over-routing costs only latency (still correct output). Safety bias.
    band_order = ["TRIVIAL", "MODERATE", "COMPLEX", "FRONTIER"]
    max_score = max(scores.values())

    if max_score == 0:
        # No signals at all — default to MODERATE (safe middle ground)
        band = ComplexityBand.MODERATE
        confidence = 0.3
        reasons.append("no strong signals, defaulting to MODERATE")
    else:
        # Among bands with the max score, pick the HIGHEST complexity one
        # This is the safety bias: better to over-route than under-route
        for b in reversed(band_order):
            if scores[b] == max_score:
                band = ComplexityBand(b)
                break

        # Confidence based on margin between winner and runner-up
        sorted_scores = sorted(scores.values(), reverse=True)
        margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
        confidence = min(0.95, 0.5 + (margin * 0.1))

    return ClassificationResult(
        band=band,
        confidence=confidence,
        reasoning="; ".join(reasons) if reasons else "keyword scoring",
        scores=scores,
    )
