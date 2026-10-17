from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from backend.blackboard import graph_store
from backend.competition.service import CompetitionService
from backend.competition.store import CompetitionStore, new_id
from backend.persistence.database import Database

pytestmark = pytest.mark.postgres


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def read_log(self, _kind, _project_id, *, limit=160):
        del limit
        return []

    def project(self, event, _project_id, **fields):
        self.events.append((event, fields))


class _Config:
    member = None
    limits = SimpleNamespace(max_concurrent_tasks=10)

    @staticmethod
    def startup_errors():
        return []

    @staticmethod
    def available_members():
        return [SimpleNamespace(name=name) for name in ("amber", "agate", "topaz", "pearl")]


@pytest.fixture
def harness(tmp_path):
    db = Database().configure()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,created_at,updated_at)
               VALUES ('wf','test','test','{}','digest',now(),now())"""
        )
    state = SimpleNamespace(
        db=db,
        instance_id="collab-coordinator",
        artifact_root=tmp_path / "artifacts",
        wp_dir=tmp_path / "artifacts" / "writeups",
        config=_Config(),
        logger=_Logger(),
        orchestrator=None,
    )
    state.artifact_root.mkdir(parents=True, exist_ok=True)
    state.wp_dir.mkdir(parents=True, exist_ok=True)
    service = CompetitionService(state, tick_interval=0.01)
    service._message_delivery_window = 0
    try:
        yield SimpleNamespace(db=db, store=service.store, service=service, state=state)
    finally:
        service.shutdown()
        db.close()


def _running_run(store: CompetitionStore):
    run = store.create_run("wf", "platform/game/team", "collab-start", {})
    run = store.transition_run(run["id"], "importing", revision=run["revision"])
    return store.transition_run(run["id"], "running", revision=run["revision"])


def _live_receiver_seat(harness, run, member="jade", external_id="c1"):
    """A ready challenge linked to a project with one live receiver seat.

    ``assign`` creates the agent_sessions row the assignment FK requires and the
    session_id that ``undelivered_messages`` joins on.
    """
    store = harness.store
    challenge = store.register_challenge(run["id"], external_id, {})
    assert store.mark_ready(challenge["id"])
    with harness.db.connect() as connection:
        project_id = graph_store.create_project(
            connection, f"Challenge {external_id}", "fixture://c", "capture the flag", "pwn"
        )
    store.link_project(challenge["id"], project_id)
    assignment = store.assign(run["id"], challenge["id"], member, "owner", role="primary")
    return challenge, project_id, assignment["session_id"]


def _insert_recon(harness, run, challenge, session_id, *, report, sender="topaz", age_seconds=600):
    envelope = {
        "run_id": run["id"],
        "challenge_id": challenge["id"],
        "message_type": "recon",
        "sender_member_id": sender,
        "receiver_member_id": "jade",
        "receiver_session_id": session_id,
        "payload": {"kind": "difficulty_report", "report": report},
    }
    with harness.db.connect() as connection:
        row = connection.execute(
            """INSERT INTO competition_messages
               (run_id,challenge_id,request_id,envelope,status,created_at)
               VALUES (%s,%s,%s,%s,'queued',now()-make_interval(secs => %s))
               RETURNING sequence""",
            (run["id"], challenge["id"], new_id(), Jsonb(envelope), age_seconds),
        ).fetchone()
    return int(row["sequence"])


def _events(store, run_id, kind):
    return [e for e in store.run_events(run_id) if e["kind"] == kind]


def test_undelivered_recon_degrades_to_blackboard_and_emits_event(harness):
    run = _running_run(harness.store)
    challenge, project_id, session_id = _live_receiver_seat(harness, run)
    report = {
        "member": "topaz",
        "progress": "Exploit developed and verified locally; blocked on the dynamic instance.",
        "knowledge": [
            "sign.bin is a 64-bit PIE ELF with no canary.",
            "Payload b'A'*0x6c + p32(0xaddaaaaa) returns a root shell locally.",
        ],
        "directions": [
            "Wait for the queued instance then run solve.py.",
        ],
    }
    _insert_recon(harness, run, challenge, session_id, report=report)

    harness.service._check_message_delivery(run)

    undelivered = _events(harness.store, run["id"], "competition.message_undelivered")
    assert len(undelivered) == 1
    assert undelivered[0]["payload"]["session_id"] == session_id

    with harness.db.connect() as connection:
        hints = graph_store.list_hints(connection, project_id)
    hint_text = "\n".join(h.content for h in hints)
    assert "no canary" in hint_text
    assert "root shell" in hint_text
    assert "run solve.py" in hint_text


def test_undelivered_injection_is_idempotent_across_ticks(harness):
    run = _running_run(harness.store)
    challenge, project_id, session_id = _live_receiver_seat(harness, run)
    _insert_recon(
        harness, run, challenge, session_id,
        report={"member": "topaz", "knowledge": ["one durable finding"]},
    )

    harness.service._check_message_delivery(run)
    harness.service._check_message_delivery(run)
    harness.service._check_message_delivery(run)

    assert len(_events(harness.store, run["id"], "competition.message_undelivered")) == 1
    with harness.db.connect() as connection:
        hints = graph_store.list_hints(connection, project_id)
    assert sum("one durable finding" in h.content for h in hints) == 1


def test_fresh_recon_within_window_is_not_flagged(harness):
    run = _running_run(harness.store)
    challenge, project_id, session_id = _live_receiver_seat(harness, run)
    harness.service._message_delivery_window = 300
    _insert_recon(
        harness, run, challenge, session_id,
        report={"member": "topaz", "knowledge": ["too fresh to strand"]},
        age_seconds=5,
    )

    harness.service._check_message_delivery(run)

    assert _events(harness.store, run["id"], "competition.message_undelivered") == []
    with harness.db.connect() as connection:
        hints = graph_store.list_hints(connection, project_id)
    assert hints == []


def test_recon_for_released_seat_is_not_flagged(harness):
    run = _running_run(harness.store)
    challenge, project_id, session_id = _live_receiver_seat(harness, run)
    with harness.db.connect() as connection:
        connection.execute(
            "UPDATE competition_assignments SET released_at=now() WHERE session_id=%s",
            (session_id,),
        )
    _insert_recon(
        harness, run, challenge, session_id,
        report={"member": "topaz", "knowledge": ["stale direction"]},
    )

    harness.service._check_message_delivery(run)

    assert _events(harness.store, run["id"], "competition.message_undelivered") == []
    with harness.db.connect() as connection:
        hints = graph_store.list_hints(connection, project_id)
    assert hints == []


def test_cursor_advance_reports_message_consumed(harness):
    run = _running_run(harness.store)
    challenge, _project_id, session_id = _live_receiver_seat(harness, run)
    sequence = _insert_recon(
        harness, run, challenge, session_id,
        report={"member": "topaz", "knowledge": ["eventually consumed"]},
    )

    harness.service._check_message_delivery(run)
    assert len(_events(harness.store, run["id"], "competition.message_undelivered")) == 1
    assert _events(harness.store, run["id"], "competition.message_consumed") == []

    harness.store.advance_message_cursor(session_id, sequence)

    harness.service._check_message_delivery(run)
    consumed = _events(harness.store, run["id"], "competition.message_consumed")
    assert len(consumed) == 1
    assert consumed[0]["payload"]["cursor"] == sequence
