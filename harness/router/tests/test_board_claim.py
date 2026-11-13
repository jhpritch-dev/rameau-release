"""The three board additions that make cross-machine dispatch to Igor possible.

Bifrost cannot see Hearth's F: share, so the board carries the brief BODY, workers
claim only their own queue, and a claim is compare-and-set so two pollers cannot
both take the same task.
"""
import asyncio

import pytest
from fastapi import HTTPException

import arbiter


@pytest.fixture(autouse=True)
def board_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(arbiter, "BOARD_DIR", str(tmp_path))
    monkeypatch.setattr(arbiter, "BOARD_FILE", str(tmp_path / "board.json"))
    yield


def mk(**body):
    body.setdefault("title", "t")
    return asyncio.run(arbiter.board_create(body))


# ----------------------------------------------------------------- brief body

def test_the_brief_body_travels_with_the_task():
    """A brief_path is useless across a machine boundary with no shared filesystem."""
    mk(id="a", brief="Do the thing.\nThen the other thing.")
    board = arbiter._board_load()
    assert board["tasks"]["a"]["brief"] == "Do the thing.\nThen the other thing."


def test_an_oversized_brief_is_refused_with_413():
    """The whole board is rewritten on every update, so this has to be bounded."""
    with pytest.raises(HTTPException) as e:
        mk(id="big", brief="x" * (arbiter.BOARD_BRIEF_MAX + 1))
    assert e.value.status_code == 413
    assert str(arbiter.BOARD_BRIEF_MAX) in e.value.detail


def test_a_brief_at_the_limit_is_accepted():
    mk(id="edge", brief="x" * arbiter.BOARD_BRIEF_MAX)
    assert len(arbiter._board_load()["tasks"]["edge"]["brief"]) == arbiter.BOARD_BRIEF_MAX


def test_a_non_string_brief_is_refused():
    with pytest.raises(HTTPException) as e:
        mk(id="bad", brief={"not": "a string"})
    assert e.value.status_code == 422


# ------------------------------------------------------------- own queue only

def test_a_worker_sees_only_its_own_queue():
    mk(id="mine", assigned_to="igor")
    mk(id="theirs", assigned_to="kilo")
    mk(id="nobodys")
    r = asyncio.run(arbiter.board_ready(assigned_to="igor"))
    assert [t["id"] for t in r["ready"]] == ["mine"]


def test_unassigned_work_is_not_picked_up_by_whoever_polls_first():
    """Unassigned must mean nobody's, not everybody's -- otherwise two workers race."""
    mk(id="nobodys")
    assert asyncio.run(arbiter.board_ready(assigned_to="igor"))["ready"] == []
    # ...but it is still visible on the unfiltered board, so it cannot be lost.
    assert [t["id"] for t in asyncio.run(arbiter.board_ready())["ready"]] == ["nobodys"]


# ------------------------------------------------------------ compare-and-set

def test_two_workers_cannot_both_claim_the_same_task():
    """Without if_phase the second claim silently overwrites the first."""
    mk(id="hot", assigned_to="igor")
    first = asyncio.run(arbiter.board_update(
        "hot", {"phase": "running", "if_phase": "pending", "assigned_to": "igor-1"}))
    assert first["phase"] == "running"

    with pytest.raises(HTTPException) as e:
        asyncio.run(arbiter.board_update(
            "hot", {"phase": "running", "if_phase": "pending", "assigned_to": "igor-2"}))
    assert e.value.status_code == 409
    assert "pending" in e.value.detail

    # the first claim survived
    assert arbiter._board_load()["tasks"]["hot"]["assigned_to"] == "igor-1"


def test_a_claim_without_if_phase_still_works():
    """Compare-and-set is opt-in; existing callers keep working."""
    mk(id="plain")
    assert asyncio.run(arbiter.board_update("plain", {"phase": "running"}))["phase"] == "running"


def test_a_bad_if_phase_value_is_refused():
    mk(id="x")
    with pytest.raises(HTTPException) as e:
        asyncio.run(arbiter.board_update("x", {"phase": "done", "if_phase": "nonsense"}))
    assert e.value.status_code == 422


def test_if_phase_on_a_missing_task_is_still_a_404():
    """Order matters: not-found must win over conflict, or a typo reads as a race."""
    with pytest.raises(HTTPException) as e:
        asyncio.run(arbiter.board_update("ghost", {"phase": "done", "if_phase": "pending"}))
    assert e.value.status_code == 404
