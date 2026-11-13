"""
test_breach_guard_followups_router.py -- Unit tests for F-2 (fan_out sibling cancellation),
F-3 (job worker typed breach marker), and F-4 (bifrost_delegate structured error).

Runs offline with zero live model calls or live network requests.
Uses standard asyncio.run() for test functions.
"""
import asyncio
import os
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

# Ensure repo root is on sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import autopilot_graph as apg
from bifrost_message import SubtaskResult, SubtaskSpec
import bifrost_delegate as bd
import igor_tool_loop as itl


# ---------------------------------------------------------------------------
# F-2: autopilot_graph.fan_out_node cancels sibling legs on tool_gate_breach
# ---------------------------------------------------------------------------

def test_fanout_sibling_cancellation_on_breach():
    """When a subtask returns error_kind='tool_gate_breach', fan_out_node must:
    1. Cancel sibling legs and mark them halted
    2. Halt remaining DAG subtasks
    3. Return status='FAILED'
    4. Route to 'failed' in route_after_fan_out
    """
    subtasks = {
        "leg1": SubtaskSpec(
            type="subtask_spec", graph_id="test-breach-graph", subtask_id="leg1",
            description="desc 1", acceptance_criteria=["crit 1"], assigned_tier="1b"
        ),
        "leg2": SubtaskSpec(
            type="subtask_spec", graph_id="test-breach-graph", subtask_id="leg2",
            description="desc 2", acceptance_criteria=["crit 2"], assigned_tier="1b"
        ),
        "leg3": SubtaskSpec(
            type="subtask_spec", graph_id="test-breach-graph", subtask_id="leg3",
            description="desc 3", acceptance_criteria=["crit 3"], assigned_tier="1b", dependencies=["leg1"]
        ),
    }
    dag_edges = {"leg1": ["leg3"]}

    def mock_run_subtask(spec):
        if spec.subtask_id == "leg1":
            return SubtaskResult(
                type="subtask_result",
                subtask_id="leg1",
                graph_id="test-breach-graph",
                status="FAILED_NEEDS_HUMAN",
                output="",
                output_checksum="0" * 64,
                verification_level_reached=0,
                tier_used="1b",
                tokens_used=0,
                cloud_cost_usd=0.0,
                escalation_artifact={
                    "error": "execute [1b]: ToolGateBreach: denied",
                    "error_kind": "tool_gate_breach",
                },
            )
        import time
        time.sleep(0.05)
        return SubtaskResult(
            type="subtask_result",
            subtask_id="leg2",
            graph_id="test-breach-graph",
            status="PASS",
            output="ok",
            output_checksum="0" * 64,
            verification_level_reached=3,
            tier_used="1b",
            tokens_used=10,
            cloud_cost_usd=0.0,
        )

    state = {
        "graph_id": "test-breach-graph",
        "trace_id": "trace-1",
        "subtasks": subtasks,
        "dag_edges": dag_edges,
        "completed": {},
        "failed": {},
        "cloud_cost_usd": 0.0,
        "status": "EXECUTING",
        "human_review_required": False,
    }

    with mock.patch.object(apg, "_run_subtask_with_semaphore", side_effect=mock_run_subtask):
        res = apg.fan_out_node(state)

    # 1. Status must be FAILED, not EXECUTING
    assert res["status"] == "FAILED", f"Expected FAILED, got {res.get('status')}"
    assert "tool gate breached" in res.get("error", "")

    # 2. Breached leg is recorded in failed
    assert "leg1" in res["failed"]

    # 3. Sibling leg2 or dependent leg3 must be recorded as halted/aborted
    assert any("halted" in v or "tool_gate_breach" in v for v in res["failed"].values())

    # 4. route_after_fan_out must route to 'failed'
    state["status"] = res["status"]
    assert apg.route_after_fan_out(state) == "failed"


# ---------------------------------------------------------------------------
# F-3: igor_tool_loop._run_job_worker records error_kind='tool_gate_breach'
# ---------------------------------------------------------------------------

def test_job_worker_typed_breach_marker(tmp_path, monkeypatch):
    """When run_tool_loop raises ToolGateBreach, the job record and collect_job
    must carry error_kind='tool_gate_breach'."""
    monkeypatch.setattr(itl, "_JOBS_DIR", str(tmp_path / "jobs"))

    async def mock_run_tool_loop_breach(**kwargs):
        raise itl.ToolGateBreach("policy denied tool: delegate_subtask")

    job_id = "job-breach-test"
    rec = {"job_id": job_id, "status": "submitted"}
    itl._job_write(rec)

    with mock.patch.object(itl, "run_tool_loop", side_effect=mock_run_tool_loop_breach):
        asyncio.run(itl._run_job_worker(job_id, {}))

    poll = itl.collect_job(job_id)
    assert poll["ok"] is True
    assert poll["status"] == "failed"
    assert poll.get("error_kind") == "tool_gate_breach", f"Missing typed error_kind: {poll}"
    assert "ToolGateBreach" in poll["error"]


def test_job_worker_regular_error_has_no_breach_marker(tmp_path, monkeypatch):
    """When run_tool_loop raises standard error, error_kind is not tool_gate_breach."""
    monkeypatch.setattr(itl, "_JOBS_DIR", str(tmp_path / "jobs"))

    async def mock_run_tool_loop_err(**kwargs):
        raise ValueError("ordinary failure")

    job_id = "job-regular-test"
    rec = {"job_id": job_id, "status": "submitted"}
    itl._job_write(rec)

    with mock.patch.object(itl, "run_tool_loop", side_effect=mock_run_tool_loop_err):
        asyncio.run(itl._run_job_worker(job_id, {}))

    poll = itl.collect_job(job_id)
    assert poll["ok"] is True
    assert poll["status"] == "failed"
    assert poll.get("error_kind") != "tool_gate_breach"


# ---------------------------------------------------------------------------
# F-4 (client): bifrost_delegate retains Router structured error object
# ---------------------------------------------------------------------------

def test_bifrost_delegate_structured_error(monkeypatch):
    """bifrost_delegate.delegate preserves HTTP status and structured error object."""
    fake_error_payload = {
        "error": {
            "message": "Tool execution denied by policy",
            "type": "bifrost_tool_gate_breach",
            "code": "TOOL_GATE_BREACH",
            "details": {"tool": "rm_rf"},
        }
    }

    mock_resp = httpx.Response(
        status_code=400,
        json=fake_error_payload,
        request=httpx.Request("POST", bd.ROUTER_CHAT),
    )

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return mock_resp

    monkeypatch.setattr(bd.httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(bd, "_LIVE_ALIASES", frozenset(bd.SAFE_LEAVES))

    leaf = sorted(bd.SAFE_LEAVES)[0]
    res = asyncio.run(bd.delegate(leaf, "ping"))

    assert res["ok"] is False
    assert res.get("status") == 400, f"Expected status 400, got {res.get('status')}"
    err = res.get("error")
    assert isinstance(err, dict), f"Expected error to be dict, got {type(err)}: {err}"
    assert err.get("code") == "TOOL_GATE_BREACH"
    assert err.get("type") == "bifrost_tool_gate_breach"

