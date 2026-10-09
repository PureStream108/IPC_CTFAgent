"""Graded watchdog over stalled agent sessions (P5c).

The behaviour under test is a judgement call, not a mechanism: the cost of
being wrong is asymmetric. Killing a Member thirty seconds into a legitimate
three-minute provider turn destroys real work, while waiting one extra interval
on a dead one costs almost nothing. So these tests pin the *reluctance* as much
as the escalation - especially that a running long job is never escalated,
since that is exactly the work P4a exists to make possible.
"""
from __future__ import annotations

import pytest

from backend.agent.watchdog import (
    CANCEL_AFTER,
    DIAGNOSE_AFTER,
    HANDOFF_AFTER,
    classify,
    diagnosis,
)


def health(**overrides):
    row = {
        "session_id": "s1",
        "task_key": "competition:run:ch",
        "owner": "worker-a",
        "epoch": 1,
        "worker_live": True,
        "quiet_seconds": 0,
        "running_jobs": 0,
        "last_event_at": None,
        "last_event_kind": None,
        "current_turn": 3,
        "turn_status": "running",
        "current_turn_started_at": None,
        "last_renewed_at": None,
    }
    row.update(overrides)
    return row


def test_a_progressing_session_is_left_entirely_alone():
    verdict = classify(health(quiet_seconds=10))
    assert verdict.level == "ok"
    assert verdict.actionable is False


def test_a_slow_provider_turn_is_not_treated_as_a_stall():
    """A single turn can legitimately run for minutes; the read timeout is 300s."""
    assert classify(health(quiet_seconds=DIAGNOSE_AFTER - 1)).level == "ok"


def test_escalation_is_graded_rather_than_binary():
    assert classify(health(quiet_seconds=DIAGNOSE_AFTER)).level == "diagnose"
    assert classify(health(quiet_seconds=CANCEL_AFTER)).level == "cancel"
    assert classify(health(quiet_seconds=HANDOFF_AFTER)).level == "handoff"


def test_a_running_long_job_is_never_escalated_past_diagnosis():
    """P4a's whole purpose is work that looks idle for hours.

    A brute force that polls its job is making progress; escalating it would
    re-impose the 60s ceiling P4a removed.
    """
    for quiet in (DIAGNOSE_AFTER, CANCEL_AFTER, HANDOFF_AFTER, HANDOFF_AFTER * 10):
        verdict = classify(health(quiet_seconds=quiet, running_jobs=1))
        assert verdict.level == "diagnose", quiet
        assert "job(s) still running" in verdict.reason


def test_a_lapsed_worker_lease_escalates_sooner_than_mere_quiet():
    """A worker that stopped renewing is gone, not slow.

    Renewal happens per turn and per tool call, so a lapsed lease is much
    stronger evidence of death than silence alone.
    """
    quiet = CANCEL_AFTER
    assert classify(health(quiet_seconds=quiet, worker_live=True)).level == "cancel"
    assert classify(health(quiet_seconds=quiet, worker_live=False)).level == "handoff"
    # Below the cancel threshold even a lapsed lease only earns a diagnosis:
    # the seat may be mid-handoff rather than dead.
    assert classify(
        health(quiet_seconds=DIAGNOSE_AFTER, worker_live=False)
    ).level == "diagnose"


def test_a_missing_worker_row_does_not_trigger_handoff_on_its_own():
    """``worker_live`` is NULL for a seat with no writer row.

    Absence means an unmodelled shape, not a dead worker, so it must not be
    the thing that takes a seat away.
    """
    assert classify(health(quiet_seconds=CANCEL_AFTER, worker_live=None)).level == "cancel"


def test_the_verdict_carries_the_evidence_behind_it():
    row = health(
        quiet_seconds=CANCEL_AFTER, current_turn=7, turn_status="running",
        last_event_kind="tool_call", running_jobs=0,
    )
    record = diagnosis(row)
    # A postmortem has to be able to tell "we killed a working Member" from
    # "the Member was gone", which needs the provider/tool state, not a verdict.
    assert record["current_turn"] == 7
    assert record["turn_status"] == "running"
    assert record["last_event_kind"] == "tool_call"
    assert record["quiet_seconds"] == CANCEL_AFTER
    assert record["session_id"] == "s1"


def test_quiet_seconds_is_read_defensively():
    # The view returns NULL for a session with no activity at all; that is 0
    # seconds of quiet, not a crash in the watchdog.
    assert classify(health(quiet_seconds=None)).level == "ok"
