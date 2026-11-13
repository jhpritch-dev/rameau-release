"""
Tests for autopilot profile field plumbing (Lane G9 / BRIEF-gemini-g9-autopilot-profile-plumbing-2026-09-11).
Validates that /autopilot/submit validates profile, stamps it into job registry, and forwards it to run_autopilot.
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
import main
from main import app, _AUTOPILOT_JOBS, _AUTOPILOT_JOBS_LOCK


@pytest.fixture
def client():
    return TestClient(app)


def test_profile_valid_values_accepted_and_lowercased(client):
    """Valid profiles are accepted (including uppercase) and lowercased in registry and run_autopilot."""
    test_cases = [
        ("research", "research"),
        ("orchestrator", "orchestrator"),
        ("coding", "coding"),
        ("general", "general"),
        ("RESEARCH", "research"),
        ("Orchestrator", "orchestrator"),
        ("CODING", "coding"),
        ("General", "general"),
    ]

    for input_val, expected_val in test_cases:
        with patch("main.run_autopilot") as mock_run:
            mock_run.return_value = {"status": "COMPLETE", "assembled_output": "ok"}
            response = client.post(
                "/autopilot/submit",
                json={"prompt": "test task", "profile": input_val},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "QUEUED"
            graph_id = data["graph_id"]

            with _AUTOPILOT_JOBS_LOCK:
                job = _AUTOPILOT_JOBS.get(graph_id)
                assert job is not None
                assert "profile" in job, "profile field missing from job registry"
                assert job["profile"] == expected_val


def test_profile_absent_and_null_defaults_to_none(client):
    """Absent or null profile is accepted and defaults to None."""
    for payload in [{"prompt": "test prompt"}, {"prompt": "test prompt", "profile": None}]:
        with patch("main.run_autopilot") as mock_run:
            mock_run.return_value = {"status": "COMPLETE", "assembled_output": "ok"}
            response = client.post("/autopilot/submit", json=payload)
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "QUEUED"
            graph_id = data["graph_id"]

            with _AUTOPILOT_JOBS_LOCK:
                job = _AUTOPILOT_JOBS.get(graph_id)
                assert job is not None
                assert "profile" in job, "profile field missing from job registry"
                assert job["profile"] is None


def test_profile_bad_type_rejected_400(client):
    """Non-string profile types return HTTP 400 and queue nothing."""
    bad_types = [123, True, False, ["research"], {"name": "research"}]
    for bad_val in bad_types:
        with patch("main.run_autopilot") as mock_run:
            initial_count = len(_AUTOPILOT_JOBS)
            response = client.post(
                "/autopilot/submit",
                json={"prompt": "test prompt", "profile": bad_val},
            )
            assert response.status_code == 400
            err = response.json().get("error", "")
            assert "profile" in err
            assert "research" in err
            assert len(_AUTOPILOT_JOBS) == initial_count
            mock_run.assert_not_called()


def test_profile_bad_value_rejected_400(client):
    """Unknown profile strings return HTTP 400 and queue nothing."""
    bad_values = ["fast", "invalid", "", "unknown", "1a-hearth"]
    for bad_val in bad_values:
        with patch("main.run_autopilot") as mock_run:
            initial_count = len(_AUTOPILOT_JOBS)
            response = client.post(
                "/autopilot/submit",
                json={"prompt": "test prompt", "profile": bad_val},
            )
            assert response.status_code == 400
            err = response.json().get("error", "")
            assert "profile" in err
            assert "research" in err
            assert len(_AUTOPILOT_JOBS) == initial_count
            mock_run.assert_not_called()


def test_profile_passed_to_run_autopilot_worker(client):
    """Directly test that _autopilot_job_runner forwards profile to run_autopilot."""
    with patch("main.run_autopilot") as mock_run:
        mock_run.return_value = {"status": "COMPLETE", "assembled_output": "done"}
        with _AUTOPILOT_JOBS_LOCK:
            _AUTOPILOT_JOBS["test-gid-123"] = {"status": "QUEUED"}

        main._autopilot_job_runner(
            graph_id="test-gid-123",
            prompt="do work",
            messages=[],
            preserve_thinking=False,
            leg_target=None,
            warp_seed=None,
            profile="coding",
        )

        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("profile") == "coding"
