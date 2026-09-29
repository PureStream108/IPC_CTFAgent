"""Integration invariants; run only with an isolated IPC_TEST_DATABASE_URL."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import zmq

from backend.blackboard import graph_store
from backend.competition.store import CompetitionConflict, CompetitionStore
from backend.competition.transport import Envelope, ReconPublisher, ReconSubscriber
from backend.competition.workspace import SharedWorkspace
from backend.core.orchestrator import Orchestrator
from backend.persistence.database import Database

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
    # A stale competition owner cannot clear the new legacy lease.
    assert not store.release_engine_lease(
        project_id, "competition", "coordinator-a", epoch=competition["engine_epoch"]
    )
    assert store.engine_lease(project_id)["engine_kind"] == "legacy"

    # Reclaiming an expired lease with the same owner still fences the old
    # worker.  Owner identity alone is not a sufficient fencing token.
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

    # Expiry/reclaim bumps the fence instead of accumulating a second active
    # assignment for the same Member/session.
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
    with pytest.raises(Exception, match="immutable"):
        with store.db.connect() as c:
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
    # The ordinary expiry pass must see the newly fenced lease as live.
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
    args = dict(assignment_id=assignment["id"], owner="worker", epoch=assignment["epoch"])
    event = store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "pwd"}, **args)
    assert store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "pwd"}, **args) == event
    with pytest.raises(CompetitionConflict):
        store.append_event(assignment["session_id"], "call-1", "tool_call", {"command": "ls"}, **args)
    store.release(assignment["id"], "worker", assignment["epoch"])
    with pytest.raises(CompetitionConflict):
        store.append_event(assignment["session_id"], "call-2", "tool_call", {}, **args)
    assert len(store.events(assignment["session_id"])) == 1


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


def test_correct_submission_creates_original_author_wp_job(store):
    run = running(store)
    challenge = ready(store, run)
    assignment = store.assign(run["id"], challenge["id"], "agate", "worker")
    queued = store.queue_candidate(assignment, "flag{correct}", "verified exploit", 0)
    claimed = store.claim_submission(run["id"])
    store.finish_submission(
        claimed["id"], {"verdict": "correct", "submission_id": "remote-1"}
    )

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
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)
            holding.set()

    holder = threading.Thread(target=hold_the_run_shard, daemon=True)
    holder.start()
    try:
        assert holding.wait(10)
        assert not failure
        # Would block until `release` if recon still took the shared lock.
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
        except BaseException as exc:  # pragma: no cover - surfaced below
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=persist, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert not failures
    assert len(results) == 4
    # One row, one sequence: every racing writer observed the same record.
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
        except BaseException as exc:  # pragma: no cover - surfaced below
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

        # Start after publication: a live-only subscriber would miss this,
        # while the durable replay path must deliver it immediately.
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
        # Retries publish the same durable record and must not create a second
        # sequence, even if the live socket announces it twice.
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
            # The callback deliberately returns out of order; the subscriber
            # emits the lowest unacknowledged sequence first.
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
    sender = store.assign(run["id"], challenge["id"], "amber", "bridge-a")
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

    # The bridge filters by project_id, so attach the challenge to the
    # project represented by the report before checking delivery.
    with store.db.connect() as connection:
        connection.execute(
            "UPDATE competition_challenges SET project_id=%s WHERE id=%s",
            (project_id, challenge["id"]),
        )
    # Re-run after linking the durable project; the first call is intentionally
    # a no-op and should not fabricate a message for an unrelated project.
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
