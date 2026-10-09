"""Invariants of the unified agent session store.

Two properties matter most here and are hard to get right: the writer fence
(only one owner may extend a transcript, and a handoff moves that right
without breaking the transcript) and the append-only event log with
idempotent replay.
"""
from __future__ import annotations

import pytest

from backend.agent.session import (
    AgentSessionStore,
    SessionConflict,
    SessionWriterLost,
    competition_task_key,
    ops_task_key,
    redact_model_snapshot,
)
from backend.persistence.database import Database

pytestmark = pytest.mark.postgres


@pytest.fixture
def store():
    db = Database().configure()
    try:
        yield AgentSessionStore(db)
    finally:
        db.close()


def owned(store, task_key="ops:session-1", owner="worker-a", kind="ops"):
    session = store.create_session(kind, task_key)
    store.claim_writer(session["id"], "ops-run", owner)
    return session


def test_event_keys_are_idempotent_and_reuse_with_new_content_is_rejected(store):
    session = owned(store)
    args = dict(owner="worker-a", epoch=1)
    first = store.append_event(session["id"], "call-1", "tool_call", {"cmd": "id"}, **args)
    assert store.append_event(session["id"], "call-1", "tool_call", {"cmd": "id"}, **args) == first
    with pytest.raises(SessionConflict, match="different content"):
        store.append_event(session["id"], "call-1", "tool_call", {"cmd": "ls"}, **args)
    assert len(store.events(session["id"])) == 1


def test_writer_fence_rejects_a_stale_owner_and_a_stale_epoch(store):
    session = owned(store)
    with pytest.raises(SessionWriterLost):
        store.append_event(
            session["id"], "e1", "status", {}, owner="other-worker", epoch=1
        )
    with pytest.raises(SessionWriterLost):
        store.append_event(session["id"], "e2", "status", {}, owner="worker-a", epoch=2)
    assert store.events(session["id"]) == []


def test_expired_writer_lease_cannot_append(store):
    session = store.create_session("ops", "ops:expiring")
    store.claim_writer(session["id"], "ops-run", "worker-a", seconds=1)
    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers
               SET lease_expires_at=now()-interval '1 second' WHERE session_id=%s""",
            (session["id"],),
        )
    with pytest.raises(SessionWriterLost):
        store.append_event(session["id"], "e1", "status", {}, owner="worker-a", epoch=1)
    assert store.renew_writer(session["id"], "worker-a", 1) is False


def test_handoff_transfers_write_access_without_starting_a_new_transcript(store):
    session = owned(store)
    store.append_event(
        session["id"], "first", "provider_messages",
        {"messages": [{"role": "user", "content": "start"}]},
        owner="worker-a", epoch=1,
    )

    writer = store.handoff(session["id"], "worker-b", reason="member_handoff")
    assert writer["owner"] == "worker-b"
    assert writer["epoch"] == 2

    # The previous owner is fenced out at its old epoch.
    with pytest.raises(SessionWriterLost):
        store.append_event(
            session["id"], "stale", "status", {}, owner="worker-a", epoch=1
        )
    # The new owner continues the same transcript.
    store.append_event(
        session["id"], "second", "provider_messages",
        {"messages": [{"role": "assistant", "content": "continuing"}]},
        owner="worker-b", epoch=2,
    )
    events = store.events(session["id"])
    assert [event["kind"] for event in events] == [
        "provider_messages", "handoff", "provider_messages",
    ]
    assert [event["event_key"] for event in events][0] == "first"
    assert events[1]["payload"] == {
        "previous_owner": "worker-a", "owner": "worker-b",
        "epoch": 2, "reason": "member_handoff",
    }


def test_handoff_of_a_live_seat_can_be_restricted_to_a_dead_worker(store):
    session = owned(store)
    with pytest.raises(SessionWriterLost, match="still live"):
        store.handoff(session["id"], "worker-b", require_expired=True)
    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers
               SET lease_expires_at=now()-interval '1 second' WHERE session_id=%s""",
            (session["id"],),
        )
    assert store.handoff(session["id"], "worker-b", require_expired=True)["owner"] == "worker-b"


def test_one_active_root_session_per_task(store):
    task = competition_task_key("run-1", "chal-1")
    first = store.create_session("competition", task)
    with pytest.raises(SessionConflict, match="active root session"):
        store.create_session("competition", task)

    # A helper seat is a child session, so it does not contend for the slot.
    helper = store.create_session(
        "competition", task, parent_session_id=first["id"], lineage_reason="helper"
    )
    assert helper["parent_session_id"] == first["id"]
    assert store.active_session_for_task(task)["id"] == first["id"]

    # Retiring the root frees the task for a fresh session.
    store.set_state(first["id"], "finished")
    replacement = store.create_session("competition", task, lineage_reason="resume")
    assert replacement["id"] != first["id"]
    assert store.active_session_for_task(task)["id"] == replacement["id"]


def test_unknown_kinds_and_reasons_are_rejected_before_any_write(store):
    with pytest.raises(SessionConflict, match="kind"):
        store.create_session("scheduler", "ops:x")
    with pytest.raises(SessionConflict, match="lineage reason"):
        store.create_session("ops", "ops:x", lineage_reason="teleport")
    session = owned(store, task_key="ops:kinds")
    with pytest.raises(SessionConflict, match="event kind"):
        store.append_event(session["id"], "e1", "telemetry", {}, owner="worker-a", epoch=1)


def test_turn_usage_is_recorded_for_token_budget_calibration(store):
    session = owned(store, task_key="ops:usage")
    store.start_turn(session["id"], 1, "worker-a", 1)
    # Restarting the same turn is idempotent, not a duplicate row.
    assert store.start_turn(session["id"], 1, "worker-a", 1)["turn"] == 1
    finished = store.finish_turn(
        session["id"], 1, "completed",
        usage={"prompt_tokens": 1200, "output_tokens": 300, "reasoning_tokens": 90},
    )
    assert finished["status"] == "completed"
    assert finished["prompt_tokens"] == 1200
    assert finished["finished_at"] is not None
    assert [turn["turn"] for turn in store.turns(session["id"])] == [1]


def test_compaction_is_additive_and_never_removes_events(store):
    session = owned(store, task_key="ops:compaction")
    for index in range(3):
        store.append_event(
            session["id"], f"e{index}", "provider_messages",
            {"messages": [{"role": "user", "content": str(index)}]},
            owner="worker-a", epoch=1,
        )
    summary = {
        "verified_facts": [{"claim": "/admin is reachable", "evidence": "curl"}],
        "failed_paths": [{"approach": "sqli", "why_failed": "no injection", "do_not_retry": True}],
    }
    store.record_compaction(session["id"], 2, summary, token_estimate=4096)
    later = store.record_compaction(session["id"], 3, {"verified_facts": []})

    assert len(store.events(session["id"])) == 3
    assert store.latest_compaction(session["id"])["compaction_id"] == later["compaction_id"]
    assert store.latest_compaction(session["id"])["up_to_sequence"] == 3


def test_provider_session_identity_persists_across_reconnects(store):
    session = store.create_session("ops", ops_task_key("s-1"))
    assert session["provider_session_id"].startswith("ipc-session-")
    assert session["provider_session_state"] == "unknown"

    updated = store.record_provider_session(
        session["id"], session["provider_session_id"], "degraded"
    )
    assert updated["provider_session_id"] == session["provider_session_id"]
    assert updated["provider_session_state"] == "degraded"
    with pytest.raises(SessionConflict):
        store.record_provider_session(session["id"], "x", "teleported")


def test_model_snapshot_never_stores_credentials(store):
    assert redact_model_snapshot(
        {"model": "m", "api_key": "secret", "nested": {"token": "t", "host": "h"}}
    ) == {"model": "m", "api_key_set": True, "nested": {"token_set": True, "host": "h"}}

    session = store.create_session(
        "ops", ops_task_key("s-secret"),
        model_snapshot={"model": "m", "api_key": "sk-live-123", "host": "example"},
    )
    stored = store.session(session["id"])["model_snapshot"]
    assert stored == {"model": "m", "api_key_set": True, "host": "example"}
    assert "sk-live-123" not in str(stored)


def test_claiming_a_session_owned_by_another_worker_requires_handoff(store):
    session = owned(store, task_key="ops:claim")
    # The same owner resuming after a restart simply renews.
    assert store.claim_writer(session["id"], "ops-run", "worker-a")["epoch"] == 1
    with pytest.raises(SessionWriterLost, match="handoff"):
        store.claim_writer(session["id"], "ops-run", "worker-b")


def test_events_can_be_read_by_the_global_event_id_cursor(store):
    session = owned(store, task_key="ops:cursor")
    first = store.append_event(
        session["id"], "e1", "status", {"n": 1}, owner="worker-a", epoch=1
    )
    second = store.append_event(
        session["id"], "e2", "status", {"n": 2}, owner="worker-a", epoch=1
    )
    assert second["event_id"] > first["event_id"]
    tail = store.events_after_event_id(session["id"], first["event_id"])
    assert [event["event_key"] for event in tail] == ["e2"]
