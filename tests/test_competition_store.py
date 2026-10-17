import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import zmq
from psycopg.errors import RaiseException

from backend.blackboard import graph_store
from backend.competition.store import (
    CompetitionConflict,
    CompetitionStore,
    _redact_snapshot,
)
from backend.competition.transport import Envelope, ReconPublisher, ReconSubscriber
from backend.competition.workspace import SharedWorkspace
from backend.core.orchestrator import Orchestrator
from backend.persistence.database import Database

_recovery_logger = logging.getLogger(__name__)

pytestmark = pytest.mark.postgres


@pytest.fixture
def store():
    db = Database().configure()
    with db.connect() as c:
        c.execute("""INSERT INTO workflows
            (id,source,name,spec_json,spec_digest,created_at,updated_at)
            VALUES ('workflow','test','test','{}','digest',now(),now())""")
    try:
        yield CompetitionStore(db)
    finally:
        db.close()


def running(store, key="start"):
    run = store.create_run("workflow", "platform/game/team", key, {})
    run = store.transition_run(run["id"], "importing", revision=run["revision"])
    return store.transition_run(run["id"], "running", revision=run["revision"])


def ready(store, run, external_id="1"):
    challenge = store.register_challenge(run["id"], external_id, {})
    assert store.mark_ready(challenge["id"])
    return challenge


def test_idempotent_start_and_only_one_active_run(store):
    first = store.create_run("workflow", "platform/game/team", "start", {})
    assert store.create_run("workflow", "platform/game/team", "start", {})["id"] == first["id"]
    with pytest.raises(CompetitionConflict):
        store.create_run("workflow", "different/team", "other-start", {})


def test_run_limit_updates_validate_types_bounds_and_revision(store):
    run = running(store, "limits")
    updated = store.update_limits(
        run["id"], {"max_concurrent_tasks": "3", "remote_concurrency": 2},
        revision=run["revision"],
    )
    assert updated["limits"]["max_concurrent_tasks"] == 3
    assert updated["limits"]["remote_concurrency"] == 2
    with pytest.raises(ValueError, match="object"):
        store.update_limits(run["id"], [], revision=updated["revision"])
    with pytest.raises(ValueError, match="unknown run"):
        store.update_limits(run["id"], {"unknown": 1}, revision=updated["revision"])
    with pytest.raises(ValueError, match="integer"):
        store.update_limits(run["id"], {"total_cpu": "bad"}, revision=updated["revision"])
    with pytest.raises(ValueError, match="between"):
        store.update_limits(run["id"], {"total_cpu": 1001}, revision=updated["revision"])
    with pytest.raises(CompetitionConflict, match="stale"):
        store.update_limits(run["id"], {"total_cpu": 1}, revision=run["revision"])


def test_submission_queue_and_verdict_branches_fence_evidence_and_cooldowns(store):
    run = running(store, "submission-branches")
    challenge = ready(store, run, "submit-branches")
    assignment = store.assign(run["id"], challenge["id"], "amber", "submitter")
    with pytest.raises(ValueError, match="candidate"):
        store.queue_candidate(assignment, "", "evidence", challenge["instance_generation"])
    with pytest.raises(ValueError, match="candidate"):
        store.queue_candidate(assignment, "flag{x}", "", challenge["instance_generation"])
    with pytest.raises(CompetitionConflict, match="stale instance"):
        store.queue_candidate(assignment, "flag{x}", "evidence", challenge["instance_generation"] + 1)

    queued = store.queue_candidate(
        assignment, "flag{x}", "evidence", challenge["instance_generation"]
    )
    submitting = store.claim_submission(run["id"])
    assert submitting["id"] == queued["id"]
    wrong = store.finish_submission(queued["id"], {"verdict": "wrong"})
    assert wrong["status"] == "wrong"

    queued_again = store.queue_candidate(
        assignment, "flag{rate}", "second evidence", challenge["instance_generation"]
    )
    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_challenges SET next_submission_at=now() WHERE id=%s",
            (challenge["id"],),
        )
    store.claim_submission(run["id"])
    rate = store.finish_submission(queued_again["id"], {"verdict": "rate_limited", "retry_after": 2})
    assert rate["status"] == "rate_limited"
    with pytest.raises(ValueError, match="unknown verdict"):
        store.finish_submission(queued_again["id"], {"verdict": "wat"})

    with pytest.raises(ValueError, match="grace"):
        store.reconcile_inflight_submissions(run["id"], grace_seconds=-1)


def test_recovered_candidate_and_inflight_reconciliation_guards(store):
    run = running(store, "recovered-candidate")
    challenge = ready(store, run, "recovered")
    with pytest.raises(ValueError, match="candidate and evidence"):
        store.queue_recovered_candidate(run["id"], challenge["id"], "", "")
    with pytest.raises(CompetitionConflict, match="accepting candidates"):
        store.queue_recovered_candidate(run["id"], challenge["id"], "flag{x}", "evidence")
    no_seat = ready(store, run, "no-seat")
    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_challenges SET state='solving' WHERE id=%s",
            (no_seat["id"],),
        )
    with pytest.raises(CompetitionConflict, match="solver session"):
        store.queue_recovered_candidate(run["id"], no_seat["id"], "flag{x}", "evidence")
    assignment = store.assign(run["id"], challenge["id"], "amber", "recover")
    recovered = store.queue_recovered_candidate(
        run["id"], challenge["id"], "flag{x}", "legacy evidence"
    )
    assert recovered["status"] == "queued"
    claimed = store.claim_submission(run["id"])
    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_submissions SET submitted_at=now()-interval '2 minutes' WHERE id=%s",
            (claimed["id"],),
        )
    assert store.reconcile_inflight_submissions(run["id"], grace_seconds=30) == 1
    assert store.submission(claimed["id"])["status"] == "unknown"
    assert assignment["member"] == "amber"


def test_external_solved_marking_is_idempotent_and_reports_missing_challenges(store):
    run = running(store, "external-solved")
    challenge = ready(store, run, "external-solved")
    solved = store.mark_external_solved(challenge["id"])
    assert solved["state"] == "solved"
    assert store.mark_external_solved(challenge["id"])["state"] == "solved"
    with pytest.raises(KeyError):
        store.mark_external_solved("missing-challenge")


def test_run_transition_rejects_stale_and_invalid_terminal_transitions(store):
    run = running(store, "transition-guards")
    with pytest.raises(CompetitionConflict, match="stale revision"):
        store.transition_run(run["id"], "paused", revision=run["revision"] - 1)
    with pytest.raises(CompetitionConflict, match="invalid run transition"):
        store.transition_run(run["id"], "importing", revision=run["revision"])


def test_challenge_state_guards_reject_unknown_and_missing_rows(store):
    run = running(store, "state-guards")
    challenge = ready(store, run, "state-guards")
    with pytest.raises(ValueError, match="invalid challenge state"):
        store.set_challenge_state(challenge["id"], "running")
    changed = store.set_challenge_state(challenge["id"], "blocked")
    assert changed["state"] == "blocked"
    with pytest.raises(KeyError):
        store.set_challenge_state("missing-challenge", "ready")


def test_store_argument_and_missing_row_guards(store):
    with pytest.raises(ValueError, match="identity"):
        store.create_run("workflow", "", "", {})
    run = running(store, "argument-guards")
    challenge = ready(store, run, "argument-guards")
    with pytest.raises(ValueError, match="Member assignment"):
        store.assign(run["id"], challenge["id"], "unknown", "owner")
    with pytest.raises(ValueError, match="Member assignment"):
        store.assign(run["id"], challenge["id"], "amber", "")
    with pytest.raises(KeyError):
        store.get_run("missing-run")


def test_competition_snapshot_redaction_keeps_only_secret_presence():
    assert _redact_snapshot({
        "api_key": "secret", "nested": {"token": "t", "host": "x"},
        "items": [{"password": "p"}],
    }) == {
        "api_key_set": True,
        "nested": {"token_set": True, "host": "x"},
        "items": [{"password_set": True}],
    }


def test_engine_lease_fences_legacy_and_competition_schedulers(store):
    """The scheduler fence closes the check/start race across processes."""
    run = running(store, "engine-fence")
    challenge = ready(store, run, "engine-project")
    with store.db.connect() as connection:
        project_id = graph_store.create_project(
            connection, "Engine fence", "fixture://engine", "goal", "web"
        )
    store.link_project(challenge["id"], project_id)

    competition = store.claim_engine_lease(
        project_id, "competition", "coordinator-a", seconds=30
    )
    assert competition["engine_epoch"] == 1
    renewed = store.claim_engine_lease(
        project_id, "competition", "coordinator-a", seconds=30
    )
    assert renewed["engine_epoch"] == competition["engine_epoch"]
    assert store.claim_engine_lease(
        project_id, "legacy", "legacy-a", seconds=30
    ) is None

    with store.db.connect() as connection:
        connection.execute(
            "UPDATE projects SET engine_lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (project_id,),
        )
    legacy = store.claim_engine_lease(project_id, "legacy", "legacy-a", seconds=30)
    assert legacy["engine_epoch"] == competition["engine_epoch"] + 1
    assert not store.release_engine_lease(
        project_id, "competition", "coordinator-a", epoch=competition["engine_epoch"]
    )
    assert store.engine_lease(project_id)["engine_kind"] == "legacy"

    with store.db.connect() as connection:
        connection.execute(
            "UPDATE projects SET engine_lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (project_id,),
        )
    reclaimed = store.claim_engine_lease(project_id, "legacy", "legacy-a", seconds=30)
    assert reclaimed["engine_epoch"] == legacy["engine_epoch"] + 1
    assert not store.renew_engine_lease(
        project_id, "legacy", "legacy-a", epoch=legacy["engine_epoch"], seconds=30
    )
    assert store.renew_engine_lease(
        project_id, "legacy", "legacy-a", epoch=reclaimed["engine_epoch"], seconds=30
    )
    assert not store.release_engine_lease(
        project_id, "legacy", "legacy-a", epoch=legacy["engine_epoch"]
    )
    assert store.release_engine_lease(
        project_id, "legacy", "legacy-a", epoch=reclaimed["engine_epoch"]
    )


def test_run_observation_tracks_replay_pressure_and_engine_lease(store):
    run = running(store, "observability")
    challenge = ready(store, run, "observe")
    with store.db.connect() as connection:
        project_id = graph_store.create_project(
            connection, "Observe", "fixture://observe", "goal", "web"
        )
    store.link_project(challenge["id"], project_id)
    sender = store.assign(run["id"], challenge["id"], "amber", "observe-coordinator")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "observe-coordinator", role="helper"
    )
    assert store.claim_engine_lease(project_id, "competition", "observe-coordinator")
    now = datetime.now(timezone.utc)
    message = Envelope(
        run_id=run["id"], challenge_id=challenge["id"],
        sender_member_id="amber", receiver_member_id="agate",
        sender_session_id=sender["session_id"], receiver_session_id=receiver["session_id"],
        request_id="observe-message", lease_epoch=sender["epoch"],
        created_at=now, deadline_at=now + timedelta(minutes=5),
        message_type="recon", payload={"kind": "pressure"},
    )
    store.persist_recon_message(message)
    sample = store.observe_run(run["id"], "observe-coordinator")
    assert sample["payload"]["capacity"] == {
        "total": 10, "solving": 1, "collaborating": 1, "writing": 0, "occupied": 2,
    }
    assert sample["payload"]["replay_backlog"] == 1
    assert sample["payload"]["engine_leases"] == {"competition": 1}
    store.advance_message_cursor(receiver["session_id"], 1)
    cleared = store.observe_run(run["id"], "observe-coordinator")
    assert cleared["payload"]["replay_backlog"] == 0
    assert store.observations(run["id"], limit=2)[0]["payload"]["replay_backlog"] == 0


def test_replay_and_assignment_leases_remain_bounded_across_repeated_cycles(store):
    """A long local replay cycle leaves no unacknowledged or stale seat state."""
    run = running(store, "replay-pressure")
    challenge = ready(store, run, "replay-pressure")
    sender = store.assign(run["id"], challenge["id"], "amber", "pressure-worker")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "pressure-worker", role="helper"
    )
    now = datetime.now(timezone.utc)
    last_sequence = 0
    for index in range(1, 65):
        record = store.persist_recon_message(Envelope(
            run_id=run["id"], challenge_id=challenge["id"],
            sender_member_id="amber", receiver_member_id="agate",
            sender_session_id=sender["session_id"], receiver_session_id=receiver["session_id"],
            request_id=f"pressure-{index}", lease_epoch=sender["epoch"],
            created_at=now, deadline_at=now + timedelta(minutes=5),
            message_type="recon", payload={"index": index},
        ))
        last_sequence = record["sequence"]
        assert store.heartbeat(sender["id"], sender["lease_owner"], sender["epoch"])

    sample = store.observe_run(run["id"], "pressure-worker")
    assert sample["payload"]["replay_backlog"] == 64
    replay = store.replay_messages(receiver["session_id"], limit=100)
    assert len(replay) == 64
    assert replay[-1]["sequence"] == last_sequence
    assert store.advance_message_cursor(receiver["session_id"], last_sequence) == last_sequence
    assert store.observe_run(run["id"], "pressure-worker")["payload"]["replay_backlog"] == 0

    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_assignments SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (sender["id"],),
        )
    recovered = store.recover_assignment(sender["id"], "pressure-restarted")
    assert recovered["session_id"] == sender["session_id"]
    assert recovered["epoch"] == sender["epoch"] + 1
    assert len(store.assignments(run["id"], active_only=True)) == 2


def test_member_global_ownership_and_max_two_solvers(store):
    run = running(store)
    one, two = ready(store, run, "1"), ready(store, run, "2")
    store.assign(run["id"], one["id"], "amber", "worker-1")
    with pytest.raises(CompetitionConflict):
        store.assign(run["id"], two["id"], "amber", "worker-2")
    store.assign(run["id"], one["id"], "agate", "worker-2", role="helper")
    with pytest.raises(CompetitionConflict):
        store.assign(run["id"], one["id"], "opal", "worker-3", role="helper")


def test_release_reassignment_preserves_deadline_and_session(store):
    run = running(store)
    challenge = ready(store, run)
    first = store.assign(run["id"], challenge["id"], "amber", "worker-1")
    with store.db.connect() as c:
        original = c.execute("SELECT deadline_at FROM competition_challenges WHERE id=%s", (challenge["id"],)).fetchone()
    assert store.release(first["id"], "worker-1", first["epoch"])
    second = store.assign(run["id"], challenge["id"], "amber", "worker-2")
    assert second["session_id"] == first["session_id"]
    with store.db.connect() as c:
        assert c.execute("SELECT deadline_at FROM competition_challenges WHERE id=%s", (challenge["id"],)).fetchone() == original
    with pytest.raises(RaiseException, match="immutable"), store.db.connect() as c:
        c.execute("UPDATE competition_challenges SET first_assigned_at=now(),deadline_at=now()+interval '5 hours' WHERE id=%s", (challenge["id"],))


def test_restart_recovers_expired_assignment_without_resetting_deadline(store):
    run = running(store)
    challenge = ready(store, run, "restart")
    original = store.assign(run["id"], challenge["id"], "amber", "old-coordinator")
    with store.db.connect() as connection:
        before = connection.execute(
            "SELECT first_assigned_at,deadline_at FROM competition_challenges WHERE id=%s",
            (challenge["id"],),
        ).fetchone()
        connection.execute(
            "UPDATE competition_assignments SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (original["id"],),
        )

    recovered = store.recover_expired_assignments(run["id"], "new-coordinator")
    assert len(recovered) == 1
    assert recovered[0]["session_id"] == original["session_id"]
    assert recovered[0]["lease_owner"] == "new-coordinator"
    assert recovered[0]["epoch"] == original["epoch"] + 1
    assert store.release_expired_assignments(run["id"]) == []
    active = store.assignments(run["id"], active_only=True)
    assert active[0]["id"] == original["id"]
    with store.db.connect() as connection:
        after = connection.execute(
            "SELECT first_assigned_at,deadline_at FROM competition_challenges WHERE id=%s",
            (challenge["id"],),
        ).fetchone()
    assert after == before
    with pytest.raises(CompetitionConflict):
        store.append_event(
            original["session_id"], "stale-after-restart", "tool_call", {},
            assignment_id=original["id"], owner="old-coordinator", epoch=original["epoch"],
        )


def test_session_events_are_idempotent_and_stale_writer_is_fenced(store):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")
    args = {"assignment_id": assignment["id"], "owner": "worker", "epoch": assignment["epoch"]}
    event = store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "pwd"}, **args)
    assert store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "pwd"}, **args) == event
    with pytest.raises(CompetitionConflict):
        store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "ls"}, **args)
    store.release(assignment["id"], "worker", assignment["epoch"])
    with pytest.raises(CompetitionConflict):
        store.append_event(assignment["session_id"], "call-2", "tool_call", {}, **args)
    assert len(store.events(assignment["session_id"])) == 1


def writer_is_live(store, session_id):
    """Ask Postgres, not Python: the DSN returns timestamps as strings here."""
    with store.db.connect() as connection:
        return bool(connection.execute(
            """SELECT 1 FROM agent_session_writers
               WHERE session_id=%s AND lease_expires_at>now()""",
            (session_id,),
        ).fetchone())


def test_assign_projects_the_seat_onto_the_agent_session_writer(store):
    """A seat must come with the writer row its worker renews (P5a)."""

    run = running(store)
    challenge = ready(store, run, "writer-projection")
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker-1")

    writer = store.agent_session_writer(assignment["session_id"])
    assert writer is not None
    assert writer["worker_kind"] == "competition-assignment"
    assert writer["owner"] == assignment["lease_owner"]
    assert writer["epoch"] == assignment["epoch"]
    assert writer_is_live(store, assignment["session_id"]) is True

    assert store.release(assignment["id"], "worker-1", assignment["epoch"])
    assert writer_is_live(store, assignment["session_id"]) is False


def test_assignment_writer_follows_recovery_and_refuses_stale_renewal(store):
    """Recovery moves both fences; a superseded owner cannot renew (P5a)."""
    run = running(store)
    challenge = ready(store, run, "writer-recovery")
    original = store.assign(run["id"], challenge["id"], "amber", "old-coordinator")
    assert store.renew_assignment_writer(original) is True

    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_assignments SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (original["id"],),
        )
    recovered = store.recover_assignment(original["id"], "new-coordinator")

    writer = store.agent_session_writer(original["session_id"])
    assert writer["owner"] == "new-coordinator"
    assert writer["epoch"] == recovered["epoch"]
    assert store.renew_assignment_writer(original) is False
    assert store.renew_assignment_writer(recovered) is True


def test_competition_turns_record_usage_for_calibration(store):
    """Competition writes agent_turns so token calibration has real data (P5a)."""
    run = running(store)
    challenge = ready(store, run, "turn-usage")
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker-1")

    store.record_turn(assignment, 1, "running")
    finished = store.record_turn(
        assignment, 1, "completed",
        usage={"prompt_tokens": 1200, "output_tokens": 300},
    )
    assert finished["prompt_tokens"] == 1200
    assert finished["output_tokens"] == 300
    assert finished["finished_at"] is not None
    assert finished["owner"] == assignment["lease_owner"]

    assert store.record_turn(assignment, 1, "completed")["prompt_tokens"] == 1200

    store.save_token_budget(assignment["session_id"], {"calibration_ratio": 1.25})
    assert store.session(assignment["session_id"])["token_budget"] == {
        "calibration_ratio": 1.25
    }


def test_heartbeat_refuses_to_prop_up_a_seat_whose_worker_went_silent(store):
    """The run4/run6 failure: renewal proved the coordinator, not the worker (P5b).

    An unconditional heartbeat let a Member that had silently stopped hold a
    challenge for its whole deadline, because renewing the lease only showed
    the coordinator was still ticking.
    """
    run = running(store)
    working = ready(store, run, "worker-alive")
    silent = ready(store, run, "worker-silent")
    alive = store.assign(run["id"], working["id"], "amber", "coordinator")
    stalled = store.assign(run["id"], silent["id"], "agate", "coordinator")

    assert store.heartbeat_run_assignments(run["id"], "coordinator") == 2
    assert store.stalled_run_assignments(run["id"]) == []

    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers SET lease_expires_at=now()-interval '1 second'
               WHERE session_id=%s""",
            (stalled["session_id"],),
        )
        connection.execute(
            """UPDATE competition_assignments SET lease_expires_at=now()+interval '5 seconds'
               WHERE run_id=%s""",
            (run["id"],),
        )

    assert store.heartbeat_run_assignments(run["id"], "coordinator") == 1
    reported = store.stalled_run_assignments(run["id"])
    assert [row["id"] for row in reported] == [stalled["id"]]

    with store.db.connect() as connection:
        extended = {
            row["id"]: row["extended"]
            for row in connection.execute(
                """SELECT id, lease_expires_at>now()+interval '30 seconds' AS extended
                   FROM competition_assignments WHERE run_id=%s""",
                (run["id"],),
            ).fetchall()
        }
    assert extended[alive["id"]] is True
    assert extended[stalled["id"]] is False

    assert store.renew_assignment_writer(stalled) is True
    assert store.heartbeat_run_assignments(run["id"], "coordinator") == 2


def test_heartbeat_still_renews_a_seat_that_has_no_writer_row(store):
    """Absence of a writer row must never be read as a dead worker (P5b).

    Wrongly declaring a live seat dead loses in-flight exploit work, so an
    unmodelled shape is renewed rather than killed.
    """
    run = running(store)
    challenge = ready(store, run, "no-writer-row")
    assignment = store.assign(run["id"], challenge["id"], "amber", "coordinator")
    with store.db.connect() as connection:
        connection.execute(
            "DELETE FROM agent_session_writers WHERE session_id=%s",
            (assignment["session_id"],),
        )

    assert store.heartbeat_run_assignments(run["id"], "coordinator") == 1
    assert store.stalled_run_assignments(run["id"]) == []


def test_heartbeat_tolerates_a_handoff_in_progress(store):
    """A writer row at another epoch is a handoff, not a stall (P5b)."""
    run = running(store)
    challenge = ready(store, run, "handoff-in-flight")
    assignment = store.assign(run["id"], challenge["id"], "amber", "coordinator")
    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers
               SET owner='another-worker',epoch=epoch+5,
                   lease_expires_at=now()-interval '1 second'
               WHERE session_id=%s""",
            (assignment["session_id"],),
        )

    assert store.heartbeat_run_assignments(run["id"], "coordinator") == 1
    assert store.stalled_run_assignments(run["id"]) == []


def test_run_events_snapshots_and_member_capacity(store):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")
    first = store.append_run_event(run["id"], "assignment.started", {"member": "amber"})
    second = store.append_run_event(run["id"], "worker.output", {"text": "redacted"})

    assert [event["event_id"] for event in store.run_events(run["id"], first["event_id"])] == [
        second["event_id"]
    ]
    snapshot = store.run_snapshot(run["id"])
    assert snapshot["capacity"] == {
        "total": 10,
        "solving": 1,
        "writing": 0,
        "collaborating": 0,
        "idle": 9,
    }
    assert snapshot["challenges"][0]["assignments"][0]["id"] == assignment["id"]
    members = store.member_snapshot(run["id"])
    assert next(item for item in members if item["member"] == "amber")["state"] == "primary"
    assert next(item for item in members if item["member"] == "opal")["state"] == "idle"


def test_submission_dedupe_wrong_cooldown_and_redaction(store):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")
    queued = store.queue_candidate(assignment, "flag{wrong}", "derived from output", 0)
    duplicate = store.queue_candidate(assignment, "flag{wrong}", "derived from output", 0)
    assert duplicate["id"] == queued["id"]

    claimed = store.claim_submission(run["id"])
    assert claimed["id"] == queued["id"]
    result = store.finish_submission(claimed["id"], {"verdict": "wrong"})
    assert result["status"] == "wrong"
    assert store.claim_submission(run["id"]) is None
    assert "candidate" not in store.submission(queued["id"])
    assert store.submission(queued["id"], reveal_candidate=True)["candidate"] == "flag{wrong}"
    assert store.challenges(run["id"])[0]["wrong_count"] == 1


def test_correct_closeout_creates_original_author_wp_job(store):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "agate", "worker")
    queued = store.queue_candidate(assignment, "flag{correct}", "verified exploit", 0)
    claimed = store.claim_submission(run["id"])
    assert claimed["id"] == queued["id"]
    with pytest.raises(CompetitionConflict, match="requires a correct"), store.db.connect() as connection:
        store.finalize_correct_challenge(connection, run["id"], challenge["id"])
    store.finish_submission(
        claimed["id"], {"verdict": "correct", "submission_id": "remote-1"}
    )

    assert store.challenges(run["id"])[0]["state"] == "solving"
    with store.db.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM competition_wp_jobs WHERE challenge_id=%s", (challenge["id"],),
        ).fetchone() is None
        store.finalize_correct_challenge(connection, run["id"], challenge["id"])
        store.finalize_correct_challenge(connection, run["id"], challenge["id"])
    solved = store.challenges(run["id"])[0]
    assert solved["state"] == "solved"
    with store.db.connect() as connection:
        job = connection.execute(
            "SELECT * FROM competition_wp_jobs WHERE challenge_id=%s", (challenge["id"],)
        ).fetchone()
    assert job["member"] == "agate"
    assert job["session_id"] == assignment["session_id"]
    assert store.release(assignment["id"], "worker", assignment["epoch"])
    claimed_wp = store.claim_wp_job(run["id"], "wp-worker")
    assert claimed_wp["job"]["member"] == "agate"
    assert claimed_wp["assignment"]["session_id"] == assignment["session_id"]
    deferred = store.finish_wp_job(
        claimed_wp["assignment"], error="provider temporarily unavailable"
    )
    assert deferred["status"] == "deferred"
    assert deferred["last_error"] == "provider temporarily unavailable"


def test_pair_messages_are_authorized_idempotent_and_replayable(store):
    run = running(store)
    challenge = ready(store, run)
    sender = store.assign(run["id"], challenge["id"], "amber", "worker-a")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "worker-b", role="helper"
    )
    now = datetime.now(timezone.utc)
    message = Envelope(
        run_id=run["id"], challenge_id=challenge["id"],
        sender_member_id="amber", receiver_member_id="agate",
        sender_session_id=sender["session_id"], receiver_session_id=receiver["session_id"],
        request_id="request-1", lease_epoch=sender["epoch"],
        created_at=now, deadline_at=now + timedelta(seconds=30),
        message_type="command", payload={"task": "inspect"},
    )
    assert store.authorize_message(message)
    assert store.claim_message(message) is None
    assert store.claim_message(message)["status"] == "in_progress"
    store.finish_message(message, {"status": "success", "artifact": "shared/a.txt"})
    assert store.claim_message(message)["artifact"] == "shared/a.txt"

    replay = store.replay_messages(receiver["session_id"])
    assert len(replay) == 1
    assert replay[0]["envelope"]["request_id"] == "request-1"
    assert store.advance_message_cursor(receiver["session_id"], replay[0]["sequence"]) == replay[0]["sequence"]
    assert store.replay_messages(receiver["session_id"]) == []

    changed = message.model_copy(update={"payload": {"task": "different"}})
    with pytest.raises(CompetitionConflict, match="different message"):
        store.claim_message(changed)


def test_recon_persists_concurrently_without_a_global_lock(store):
    """Recon is the highest-volume writer: it must not serialize on one lock.

    The assertion is structural rather than timing-based: a second connection
    holding the run shard still lets a recon insert commit, which is only true
    once ``persist_recon_message`` relies on UNIQUE (run_id,request_id) instead
    of the database-wide advisory lock.
    """
    import threading

    run = running(store)
    challenge = ready(store, run, "concurrent-recon")
    sender = store.assign(run["id"], challenge["id"], "amber", "worker-a")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "worker-b", role="helper"
    )
    now = datetime.now(timezone.utc)

    def message(request_id: str) -> Envelope:
        return Envelope(
            run_id=run["id"], challenge_id=challenge["id"],
            sender_member_id="amber", receiver_member_id="agate",
            sender_session_id=sender["session_id"],
            receiver_session_id=receiver["session_id"],
            request_id=request_id, lease_epoch=sender["epoch"],
            created_at=now, deadline_at=now + timedelta(minutes=5),
            message_type="recon", payload={"value": request_id},
        )

    holding = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def hold_the_run_shard() -> None:
        try:
            with store.db.connect() as connection:
                store._lock(connection, run["id"])
                holding.set()
                release.wait(10)
        except BaseException as exc:
            _recovery_logger.debug('Error handled in hold_the_run_shard', exc_info=True)
            failure.append(exc)
            holding.set()

    holder = threading.Thread(target=hold_the_run_shard, daemon=True)
    holder.start()
    try:
        assert holding.wait(10)
        assert not failure
        record = store.persist_recon_message(message("free-recon"))
        assert record["status"] == "done"
    finally:
        release.set()
        holder.join(10)
    assert not failure


def test_concurrent_recon_of_one_request_id_stays_idempotent(store):
    """Without the lock, two writers race the unique key; both must succeed."""
    import threading

    run = running(store)
    challenge = ready(store, run, "racing-recon")
    sender = store.assign(run["id"], challenge["id"], "amber", "worker-a")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "worker-b", role="helper"
    )
    now = datetime.now(timezone.utc)
    envelope = Envelope(
        run_id=run["id"], challenge_id=challenge["id"],
        sender_member_id="amber", receiver_member_id="agate",
        sender_session_id=sender["session_id"],
        receiver_session_id=receiver["session_id"],
        request_id="raced", lease_epoch=sender["epoch"],
        created_at=now, deadline_at=now + timedelta(minutes=5),
        message_type="recon", payload={"value": "raced"},
    )

    start = threading.Barrier(4)
    results: list[dict] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def persist() -> None:
        try:
            start.wait(10)
            record = store.persist_recon_message(envelope)
            with lock:
                results.append(record)
        except BaseException as exc:
            _recovery_logger.debug('Error handled in persist', exc_info=True)
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=persist, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert not failures
    assert len(results) == 4
    assert len({record["sequence"] for record in results}) == 1


def test_two_runs_do_not_contend_on_the_same_lock_shard(store):
    """A finished run's shard must not block a new run's slot transitions."""
    import threading

    first = running(store, "first-run")
    store.transition_run(
        first["id"], "stopped", revision=first["revision"], permit_active=True
    )
    second = running(store, "second-run")
    challenge = ready(store, second, "sharded")

    holding = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def hold_first_run_shard() -> None:
        try:
            with store.db.connect() as connection:
                store._lock(connection, first["id"])
                holding.set()
                release.wait(10)
        except BaseException as exc:
            _recovery_logger.debug('Error handled in hold_first_run_shard', exc_info=True)
            failure.append(exc)
            holding.set()

    holder = threading.Thread(target=hold_first_run_shard, daemon=True)
    holder.start()
    try:
        assert holding.wait(10)
        assert not failure
        assignment = store.assign(
            second["id"], challenge["id"], "amber", "worker-a"
        )
        assert assignment["member"] == "amber"
    finally:
        release.set()
        holder.join(10)
    assert not failure


def test_recon_postgres_publisher_replays_across_offline_and_restart(store):
    """The durable row, not the live PUB socket, is the delivery source."""
    run = running(store)
    challenge = ready(store, run, "recon")
    sender = store.assign(run["id"], challenge["id"], "amber", "recon-sender")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "recon-receiver", role="helper"
    )
    now = datetime.now(timezone.utc)

    def message(request_id: str, value: str) -> Envelope:
        return Envelope(
            run_id=run["id"], challenge_id=challenge["id"],
            sender_member_id="amber", receiver_member_id="agate",
            sender_session_id=sender["session_id"],
            receiver_session_id=receiver["session_id"],
            request_id=request_id, lease_epoch=sender["epoch"],
            created_at=now, deadline_at=now + timedelta(minutes=5),
            message_type="recon", payload={"value": value},
        )

    context = zmq.Context()
    publisher = ReconPublisher(
        "inproc://competition-recon-postgres",
        store.persist_recon_message,
        context=context,
    )
    try:
        first = publisher.publish(message("recon-1", "one"))

        subscriber = ReconSubscriber(
            "inproc://competition-recon-postgres", challenge["id"],
            session_id=receiver["session_id"],
            replay=store.replay_messages,
            advance=store.advance_message_cursor,
            context=context,
        )
        try:
            record = subscriber.receive(timeout_ms=0)
            assert record["sequence"] == first["sequence"]
            assert record["envelope"]["payload"] == {"value": "one"}
            assert subscriber.ack() == first["sequence"]
        finally:
            subscriber.close()

        second = publisher.publish(message("recon-2", "two"))
        assert publisher.publish(message("recon-2", "two"))["sequence"] == second["sequence"]
        third = publisher.publish(message("recon-3", "three"))

        def reverse_replay(session_id, **kwargs):
            return list(reversed(store.replay_messages(session_id, **kwargs)))

        restarted = ReconSubscriber(
            "inproc://competition-recon-postgres", challenge["id"],
            session_id=receiver["session_id"],
            replay=reverse_replay,
            advance=store.advance_message_cursor,
            context=context,
        )
        try:
            assert restarted.receive(timeout_ms=0)["sequence"] == second["sequence"]
            assert restarted.ack() == second["sequence"]
            assert restarted.receive(timeout_ms=0)["sequence"] == third["sequence"]
            assert restarted.ack() == third["sequence"]
            assert restarted.receive(timeout_ms=0) is None
        finally:
            restarted.close()
    finally:
        publisher.close()
        context.term()


def test_orchestrator_report_bridge_uses_durable_recon_store(store):
    """The production report bridge persists one record per helper seat."""
    run = running(store, "recon-bridge")
    challenge = ready(store, run, "bridge")
    store.assign(run["id"], challenge["id"], "amber", "bridge-a")
    receiver = store.assign(
        run["id"], challenge["id"], "agate", "bridge-b", role="helper"
    )

    class Competition:
        def __init__(self):
            self.messages = []

        def publish_recon(self, message):
            self.messages.append(message)
            return store.persist_recon_message(message)

    class Logger:
        def project(self, *args, **kwargs):
            raise AssertionError(f"unexpected recon bridge error: {args} {kwargs}")

    competition = Competition()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.state = SimpleNamespace(
        db=store.db,
        competition=competition,
        logger=Logger(),
    )
    with store.db.connect() as connection:
        project_id = graph_store.create_project(
            connection,
            "Recon bridge project",
            "fixture://recon-bridge",
            "bridge recon",
            "web",
        )

    orchestrator._publish_competition_recon(
        project_id,
        "amber",
        event_id="report:bridge-1",
        payload={"kind": "difficulty_report", "progress": "shared evidence"},
    )

    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_challenges SET project_id=%s WHERE id=%s",
            (project_id, challenge["id"]),
        )
    orchestrator._publish_competition_recon(
        project_id,
        "amber",
        event_id="report:bridge-2",
        payload={"kind": "difficulty_report", "progress": "shared evidence"},
    )

    assert len(competition.messages) == 1
    replay = store.replay_messages(receiver["session_id"])
    assert len(replay) == 1
    assert replay[0]["envelope"]["receiver_member_id"] == "agate"
    assert replay[0]["envelope"]["payload"]["progress"] == "shared evidence"


def test_shared_workspace_rejects_stale_writes_without_losing_content(store, tmp_path):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")
    workspace = SharedWorkspace(store.db, tmp_path)

    first = workspace.write(
        challenge["id"], "analysis/result.txt", b"version one",
        session_id=assignment["session_id"], assignment_id=assignment["id"],
        lease_owner=assignment["lease_owner"], lease_epoch=assignment["epoch"],
        expected_version=None,
    )
    second = workspace.write(
        challenge["id"], "analysis/result.txt", b"version two",
        session_id=assignment["session_id"], assignment_id=assignment["id"],
        lease_owner=assignment["lease_owner"], lease_epoch=assignment["epoch"],
        expected_version=first["version"],
    )
    with pytest.raises(CompetitionConflict, match="version conflict"):
        workspace.write(
            challenge["id"], "analysis/result.txt", b"stale overwrite",
            session_id=assignment["session_id"], assignment_id=assignment["id"],
            lease_owner=assignment["lease_owner"], lease_epoch=assignment["epoch"],
            expected_version=first["version"],
        )
    metadata, content = workspace.read(challenge["id"], "analysis/result.txt")
    assert metadata["version"] == second["version"] == 2
    assert content == b"version two"
    with pytest.raises(ValueError, match="relative path"):
        workspace.write(
            challenge["id"], "../escape", b"bad",
            session_id=assignment["session_id"], assignment_id=assignment["id"],
            lease_owner=assignment["lease_owner"], lease_epoch=assignment["epoch"],
            expected_version=None,
        )


def test_shared_workspace_requires_live_assignment_fence_and_enforces_quota(store, tmp_path):
    run = running(store)
    challenge = ready(store, run, "fenced")
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")
    workspace = SharedWorkspace(
        store.db, tmp_path, max_total_bytes=4, min_free_bytes=0,
    )
    kwargs = {
        "session_id": assignment["session_id"],
        "assignment_id": assignment["id"],
        "lease_owner": assignment["lease_owner"],
        "lease_epoch": assignment["epoch"],
        "expected_version": None,
    }
    workspace.write(challenge["id"], "a.txt", b"1234", **kwargs)
    with pytest.raises(CompetitionConflict, match="quota"):
        workspace.write(challenge["id"], "b.txt", b"5", **kwargs)
    store.release(assignment["id"], assignment["lease_owner"], assignment["epoch"])
    with pytest.raises(CompetitionConflict, match="stale"):
        workspace.write(challenge["id"], "a.txt", b"x", **kwargs)


def test_shared_workspace_recovers_missing_or_corrupt_files_and_isolates_challenges(
    store, tmp_path
):
    run = running(store)
    first = ready(store, run, "workspace-one")
    second = ready(store, run, "workspace-two")
    first_assignment = store.assign(run["id"], first["id"], "amber", "workspace-a")
    second_assignment = store.assign(run["id"], second["id"], "agate", "workspace-b")
    workspace = SharedWorkspace(store.db, tmp_path, min_free_bytes=0)

    first_row = workspace.write(
        first["id"], "notes.txt", b"first", session_id=first_assignment["session_id"],
        assignment_id=first_assignment["id"], lease_owner=first_assignment["lease_owner"],
        lease_epoch=first_assignment["epoch"], expected_version=None,
    )
    second_row = workspace.write(
        second["id"], "notes.txt", b"second", session_id=second_assignment["session_id"],
        assignment_id=second_assignment["id"], lease_owner=second_assignment["lease_owner"],
        lease_epoch=second_assignment["epoch"], expected_version=None,
    )
    assert first_row["path"] == second_row["path"] == "notes.txt"
    assert (tmp_path / first["id"] / "shared" / "notes.txt").read_bytes() == b"first"
    assert (tmp_path / second["id"] / "shared" / "notes.txt").read_bytes() == b"second"

    first_path = tmp_path / first["id"] / "shared" / "notes.txt"
    first_path.unlink()
    with pytest.raises(CompetitionConflict, match="metadata exists"):
        workspace.read(first["id"], "notes.txt")
    workspace.write(
        first["id"], "notes.txt", b"restored", session_id=first_assignment["session_id"],
        assignment_id=first_assignment["id"], lease_owner=first_assignment["lease_owner"],
        lease_epoch=first_assignment["epoch"], expected_version=first_row["version"],
    )
    first_path.write_bytes(b"tampered")
    with pytest.raises(CompetitionConflict, match="does not match"):
        workspace.read(first["id"], "notes.txt")


def test_shared_workspace_rejects_disk_shortage_before_creating_metadata(
    store, tmp_path, monkeypatch
):
    run = running(store)
    challenge = ready(store, run, "disk-shortage")
    assignment = store.assign(run["id"], challenge["id"], "amber", "disk-worker")
    workspace = SharedWorkspace(store.db, tmp_path, min_free_bytes=10)
    monkeypatch.setattr(
        "backend.competition.workspace.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=9),
    )
    with pytest.raises(CompetitionConflict, match="insufficient disk space"):
        workspace.write(
            challenge["id"], "blocked.txt", b"content",
            session_id=assignment["session_id"], assignment_id=assignment["id"],
            lease_owner=assignment["lease_owner"], lease_epoch=assignment["epoch"],
            expected_version=None,
        )
    with store.db.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM competition_artifacts WHERE challenge_id=%s AND path=%s",
            (challenge["id"], "blocked.txt"),
        ).fetchone() is None
    assert not (tmp_path / challenge["id"] / "shared" / "blocked.txt").exists()


def test_session_health_separates_the_four_signals_the_ui_merged(store):
    """Health must distinguish coordinator liveness from actual progress (P5c)."""
    run = running(store)
    challenge = ready(store, run, "health-signals")
    assignment = store.assign(run["id"], challenge["id"], "amber", "coordinator")

    fresh = store.session_health(assignment["session_id"])
    assert fresh["worker_live"] is True
    assert fresh["event_count"] == 0
    assert fresh["quiet_seconds"] >= 0

    store.append_event(
        assignment["session_id"], "call-1", "tool_call", {"command": "id"},
        assignment_id=assignment["id"], owner=assignment["lease_owner"],
        epoch=assignment["epoch"],
    )
    store.record_turn(assignment, 1, "running")

    active = store.session_health(assignment["session_id"])
    assert active["event_count"] == 1
    assert active["last_event_kind"] == "tool_call"
    assert active["current_turn"] == 1
    assert active["turn_status"] == "running"
    assert active["current_turn_started_at"] is not None

    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers SET lease_expires_at=now()-interval '1 second'
               WHERE session_id=%s""",
            (assignment["session_id"],),
        )
    assert store.session_health(assignment["session_id"])["worker_live"] is False


def test_run_session_health_is_one_query_ordered_by_quiet_time(store):
    run = running(store)
    first = ready(store, run, "health-1")
    second = ready(store, run, "health-2")
    one = store.assign(run["id"], first["id"], "amber", "coordinator")
    two = store.assign(run["id"], second["id"], "agate", "coordinator")

    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_sessions SET created_at=now()-interval '10 minutes'
               WHERE id=%s""",
            (one["session_id"],),
        )
        connection.execute(
            """UPDATE agent_session_writers
               SET last_renewed_at=now()-interval '10 minutes'
               WHERE session_id=%s""",
            (one["session_id"],),
        )

    rows = store.run_session_health(run["id"])
    assert {row["session_id"] for row in rows} == {one["session_id"], two["session_id"]}
    assert rows[0]["session_id"] == one["session_id"]
    assert rows[0]["quiet_seconds"] > rows[1]["quiet_seconds"]
    assert rows[0]["assignment_id"] == one["id"]
    assert rows[0]["member"] == "amber"
    assert rows[1]["member"] == "agate"


def test_handoff_moves_both_fences_so_the_new_member_can_write(store):
    """The easiest thing to get wrong in P5c.

    Competition writes are fenced by the assignment lease while a handoff moves
    the writer row. Advancing only one leaves the incoming Member holding one
    fence and rejected by the other, unable to append a single event.
    """
    run = running(store)
    challenge = ready(store, run, "handoff-fences")
    original = store.assign(run["id"], challenge["id"], "amber", "coordinator")
    store.append_event(
        original["session_id"], "finding-1", "tool_result", {"evidence": "port 8080"},
        assignment_id=original["id"], owner=original["lease_owner"],
        epoch=original["epoch"],
    )

    with pytest.raises(CompetitionConflict, match="still live"):
        store.handoff_assignment(original["id"], "agate", "coordinator")

    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers SET lease_expires_at=now()-interval '1 second'
               WHERE session_id=%s""",
            (original["session_id"],),
        )

    moved = store.handoff_assignment(original["id"], "agate", "coordinator")
    assert moved["member"] == "agate"
    assert moved["epoch"] == original["epoch"] + 1
    assert moved["session_id"] == original["session_id"]

    writer = store.agent_session_writer(moved["session_id"])
    assert writer["owner"] == moved["lease_owner"]
    assert writer["epoch"] == moved["epoch"]

    store.append_event(
        moved["session_id"], "finding-2", "tool_result", {"evidence": "took over"},
        assignment_id=moved["id"], owner=moved["lease_owner"], epoch=moved["epoch"],
    )
    kinds = [row["kind"] for row in store.events(moved["session_id"])]
    assert kinds == ["tool_result", "handoff", "tool_result"]

    with pytest.raises(CompetitionConflict):
        store.append_event(
            moved["session_id"], "stale", "tool_call", {},
            assignment_id=original["id"], owner=original["lease_owner"],
            epoch=original["epoch"],
        )


def test_handoff_refuses_a_member_that_already_holds_a_seat(store):
    run = running(store)
    one = ready(store, run, "occupied-1")
    two = ready(store, run, "occupied-2")
    stalled = store.assign(run["id"], one["id"], "amber", "coordinator")
    store.assign(run["id"], two["id"], "agate", "coordinator")
    with store.db.connect() as connection:
        connection.execute(
            """UPDATE agent_session_writers SET lease_expires_at=now()-interval '1 second'
               WHERE session_id=%s""",
            (stalled["session_id"],),
        )

    with pytest.raises(CompetitionConflict, match="already occupied"):
        store.handoff_assignment(stalled["id"], "agate", "coordinator")
    assert store.handoff_assignment(stalled["id"], "amber", "coordinator")["member"] == "amber"
