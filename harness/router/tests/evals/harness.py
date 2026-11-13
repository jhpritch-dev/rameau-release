"""
evals/harness.py — Eval harness for the Bifrost self-improvement loop.

Runs a fixed set of test prompts through AUTOPILOT (autopilot_graph.run_autopilot),
captures L3-L5 pass/fail per subtask, writes scores to evals/scores.jsonl.

Integrates with the real autopilot_graph.py pipeline:
  gate -> decompose -> fan_out (subtask_graph) -> assemble -> END

Usage:
    python evals/harness.py                   # run all 10 cases
    python evals/harness.py --split train     # run only train set (8 cases)
    python evals/harness.py --split holdout   # run only holdout set (2 cases)

Scores output: evals/scores.jsonl
    Each line: {"timestamp": ..., "split": ..., "total": N, "passed": N,
                "score": 0.0-1.0, "details": [...]}
"""

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Add parent dir to path so we can import autopilot_graph
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("harness")

# ── Paths ──────────────────────────��─────────────────────────
EVALS_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALS_DIR / "cases"
TRAIN_DIR = EVALS_DIR / "train"
HOLDOUT_DIR = EVALS_DIR / "holdout"
SCORES_PATH = EVALS_DIR / "scores.jsonl"

# ── Manifest: which cases go to train vs holdout ─────────────
# 80/20 split: 8 train, 2 holdout
# Holdout: one MODERATE + one COMPLEX to cover both bands
HOLDOUT_IDS = {"moderate_004", "complex_003"}


def load_case(path: Path) -> dict:
    """Load a single eval case from JSON."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cases(split: str = "all") -> list[dict]:
    """Load eval cases for a given split.

    split="all"     -> all 10 cases from cases/
    split="train"   -> 8 cases (excluding HOLDOUT_IDS)
    split="holdout" -> 2 cases (only HOLDOUT_IDS)
    """
    cases = []
    for json_file in sorted(CASES_DIR.glob("*.json")):
        case = load_case(json_file)
        case_id = case.get("id", json_file.stem)

        if split == "train" and case_id in HOLDOUT_IDS:
            continue
        elif split == "holdout" and case_id not in HOLDOUT_IDS:
            continue

        cases.append(case)
    return cases


def run_case_through_autopilot(case: dict) -> dict:
    """
    Run a single eval case through the real AUTOPILOT pipeline.

    Uses autopilot_graph.run_autopilot() which executes:
      gate -> decompose -> fan_out -> assemble

    Returns dict with pass/fail status and metadata.
    """
    from autopilot_graph import run_autopilot

    graph_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"

    # Build the prompt from the eval case
    prompt = case["prompt"]

    # Run through AUTOPILOT
    result = run_autopilot(
        prompt=prompt,
        graph_id=graph_id,
        warp_seed={"route": "direct"},  # direct route for eval cases (single subtask)
    )

    status = result.get("status", "FAILED")
    completed = result.get("completed", {})
    failed = result.get("failed", {})
    assembled = result.get("assembled_output", "")
    cost = result.get("cloud_cost_usd", 0.0)

    # Determine pass/fail based on AUTOPILOT result status
    all_passed = status in ("COMPLETE", "PASS") and len(failed) == 0

    # Extract verification level from completed subtasks
    max_verif_level = 0
    for sid, sr in completed.items():
        vl = getattr(sr, "verification_level_reached", 0)
        if vl > max_verif_level:
            max_verif_level = vl

    # L3 = reached level 3+, L4 = reached level 4+, L5 = reached level 5+
    l3_passed = max_verif_level >= 3
    l4_passed = max_verif_level >= 4
    l5_passed = max_verif_level >= 5

    return {
        "id": case["id"],
        "band": case.get("band", "MODERATE"),
        "l3": l3_passed,
        "l4": l4_passed,
        "l5": l5_passed,
        "all_passed": all_passed and l5_passed,
        "status": status,
        "verification_level": max_verif_level,
        "cloud_cost_usd": cost,
        "subtasks_completed": len(completed),
        "subtasks_failed": len(failed),
        "output_preview": (assembled or "")[:200],
    }


def run_harness(split: str = "all", max_cases: int = 0) -> dict:
    """
    Run eval harness on the specified split.
    Returns the score record (also appended to scores.jsonl).

    Args:
        split: "all", "train", or "holdout"
        max_cases: if >0, only run the first N cases (for smoke tests)
    """
    cases = load_cases(split)
    if not cases:
        log.error(f"No cases found for split '{split}'")
        return {}

    if max_cases > 0:
        cases = cases[:max_cases]

    log.info(f"Running harness: split={split}, cases={len(cases)}")

    details = []
    passed_count = 0

    for case in cases:
        log.info(f"\n{'='*60}")
        log.info(f"Case: {case['id']} [{case.get('band', '?')}]")
        log.info(f"  {case['description']}")

        try:
            case_detail = run_case_through_autopilot(case)
        except Exception as e:
            log.error(f"  AUTOPILOT error for {case['id']}: {e}")
            case_detail = {
                "id": case["id"],
                "band": case.get("band", "MODERATE"),
                "l3": False,
                "l4": False,
                "l5": False,
                "all_passed": False,
                "failure_detail": str(e)[:200],
            }

        if case_detail.get("all_passed"):
            passed_count += 1
            log.info(f"  -> PASS (L5 verified)")
        else:
            failed_at = "L3"
            if case_detail.get("l3"):
                failed_at = "L4"
            if case_detail.get("l4"):
                failed_at = "L5"
            if not case_detail.get("l3") and not case_detail.get("l4"):
                failed_at = "AUTOPILOT"
            log.info(f"  -> FAIL at {failed_at}")

            # Capture failure detail for Better-Harness mining (P1)
            if "failure_detail" not in case_detail:
                case_detail["failure_detail"] = f"Failed at {failed_at}"

        details.append(case_detail)

    score = passed_count / len(cases) if cases else 0.0

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "split": split,
        "total": len(cases),
        "passed": passed_count,
        "score": round(score, 4),
        "details": details,
    }

    # Write to scores.jsonl
    SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SCORES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    log.info(f"\n{'='*60}")
    log.info(f"HARNESS RESULT: {passed_count}/{len(cases)} passed (score={score:.2%})")
    log.info(f"Score written to {SCORES_PATH}")

    return record


def get_last_score(split: str = "train") -> float | None:
    """Read the most recent score for a given split from scores.jsonl."""
    if not SCORES_PATH.exists():
        return None

    last = None
    with open(SCORES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("split") == split:
                    last = record.get("score")
            except json.JSONDecodeError:
                continue
    return last


def get_last_n_scores(split: str = "train", n: int = 5) -> list[float]:
    """Read the last N scores for a given split."""
    scores = []
    if not SCORES_PATH.exists():
        return scores

    with open(SCORES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("split") == split:
                    scores.append(record.get("score", 0.0))
            except json.JSONDecodeError:
                continue
    return scores[-n:]


def get_last_details(split: str = "train") -> list[dict]:
    """Read the details from the most recent harness run for a split."""
    if not SCORES_PATH.exists():
        return []

    last_details = []
    with open(SCORES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("split") == split:
                    last_details = record.get("details", [])
            except json.JSONDecodeError:
                continue
    return last_details


# ── CLI ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Bifrost eval harness")
    parser.add_argument(
        "--split",
        choices=["all", "train", "holdout"],
        default="all",
        help="Which case split to run (default: all)",
    )
    args = parser.parse_args()

    record = run_harness(args.split)
    if record:
        print(json.dumps(record, indent=2))
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
