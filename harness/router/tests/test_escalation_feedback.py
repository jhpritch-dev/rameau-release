"""The escalation retry must be TOLD why L5 rejected the previous output.

verify_subtask_node writes an L5 failure as a FLAT escalation_artifact
{"error": "L5: ...", "evidence": ...} (it REPLACES the execute-time nested `sandbox_l5`
shape). escalate_subtask_node originally read ONLY the nested `sandbox_l5` key, so on an L5
failure it found nothing -> dead_ends=None -> the retry re-rolled BLIND. Live proof
2026-07-25 (graph cd9a4e5e, router v253): a 2_qwen_coder leg imported pandas, failed L5, and
re-rolled pandas three times because it was never shown "No module named 'pandas'".
"""
from bifrost_message import SubtaskSpec, SubtaskResult
from subtask_graph import escalate_subtask_node


def _state(artifact):
    spec = SubtaskSpec(
        type="subtask_spec", graph_id="g", subtask_id="s1",
        description="Implement main.", acceptance_criteria=["x"],
        assigned_tier="2_qwen_coder", complexity_band="MODERATE",
    )
    result = SubtaskResult(
        type="subtask_result", subtask_id="s1", graph_id="g", status="FAIL",
        output="```python\nimport pandas as pd\n```", output_checksum="c",
        verification_level_reached=5, tier_used="2_qwen_coder", tokens_used=100,
        escalation_artifact=artifact,
    )
    # 2_qwen_coder is not in the MODERATE cascade, so this exercises the escalation-exhausted
    # same-tier-retry path -- the one that carried dead_ends forward as feedback.
    # tier_history is the record the tried-tier guards read since a0f7322 (A2).
    return {"spec": spec, "result": result, "attempt_count": 1, "tier_history": ["2_qwen_coder"],
            "verification_level": 5, "context_accumulator": [], "error": None}


def test_flat_l5_artifact_reaches_retry_feedback():
    out = escalate_subtask_node(_state(
        {"error": "L5: module raised on import",
         "evidence": "ModuleNotFoundError: No module named 'pandas'"}))
    acc = out["context_accumulator"]
    assert acc, "expected an escalation artifact appended"
    dead_ends = acc[-1]["dead_ends"]
    assert dead_ends is not None, "regression: retry was BLIND (dead_ends=None)"
    joined = " ".join(dead_ends)
    assert "module raised on import" in joined
    assert "No module named 'pandas'" in joined


def test_nested_sandbox_l5_artifact_still_works():
    out = escalate_subtask_node(_state(
        {"sandbox_l5": {"summary": "compile FAIL: invalid syntax", "stderr": "line 3"}}))
    joined = " ".join(out["context_accumulator"][-1]["dead_ends"])
    assert "compile FAIL: invalid syntax" in joined


def test_coder_leg_escalates_cross_model_first():
    """A 2_qwen_coder leg (absent from the cascade) escalates to the OTHER co-loaded coder,
    not a same-tier re-roll -- carrying the failure feedback.

    The cross target is 2.5, NOT 2_devstral. Until 2026-08-01 a failing coder crossed to
    devstral, but autopilot_graph then dropped devstral from code/test work ("2026-08-01:
    dropped 2_devstral; code/test -> coding tiers") because it is the agentic/analytical
    model, not a coder -- so crossing a CODE failure to it sent the repair to a model
    deliberately excluded from code. _CODER_CROSS_ESCALATION pairs the two coders instead:
    2_qwen_coder <-> 2.5. devstral is still live and still crosses TO a coder.
    """
    out = escalate_subtask_node(_state({"error": "L5: import pandas", "evidence": "no pandas"}))
    assert out["spec"].assigned_tier == "2.5"
    joined = " ".join(out["context_accumulator"][-1]["dead_ends"])
    assert "import pandas" in joined


def test_cross_model_does_not_pingpong():
    """Once the other coder has already been tried, do NOT cross back to it.

    This asserted `== "2_qwen_coder"` (a same-tier re-roll). ca40294 (#56) deliberately
    removed that: "give a chain-end CODE leg a coder, and stop re-rolling a degenerating
    tier". Re-rolling the tier that just failed was the degenerate-run bug, so pinning the
    old literal would now be asserting the presence of a fixed defect. What must hold is the
    invariant the test was named for: it does not ping-pong back to the exhausted cross, and
    it does not re-roll the tier that just failed -- it moves somewhere new and legitimate.
    """
    from subtask_graph import get_cascade

    st = _state({"error": "L5: import pandas", "evidence": "no pandas"})
    # Seed the tier the cross would target (2.5), so the _tried guard has to refuse it.
    st["context_accumulator"] = [{"tier": "2.5", "attempt": 1, "dead_ends": ["x"]}]
    st["tier_history"] = ["2.5", "2_qwen_coder"]
    out = escalate_subtask_node(st)
    landed = out["spec"].assigned_tier

    assert landed != "2.5", "ping-ponged back to the coder that was already tried"
    assert landed != "2_qwen_coder", "re-rolled the tier that just failed (degenerate run, #56)"
    assert landed in get_cascade("MODERATE"), (
        f"escalated to {landed!r}, which is not a tier in the MODERATE cascade")
