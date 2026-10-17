from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from backend.agent.job_service import JobService
from backend.agent.jobs import AgentJobStore, JobConflict
from backend.agent.session import AgentSessionStore
from backend.persistence.database import Database
from backend.sandbox.sandbox import LocalSandbox

pytestmark = pytest.mark.postgres


@pytest.fixture
def db():
    database = Database().configure()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def session(db):
    return AgentSessionStore(db).create_session("competition", "competition:run:ch")


@pytest.fixture
def jobs(db):
    return AgentJobStore(db)


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def test_detached_start_failure_never_leaves_a_running_job(jobs, session):
    def unavailable(*_args):
        raise ConnectionError("sandbox disconnected during launch")

    service = JobService(jobs, SimpleNamespace(exec_detached=unavailable), session["id"])
    assert service.start("  ")["ok"] is False
    result = service.start("sleep 300")
    assert result["ok"] is False
    row = jobs.job(result["job_id"])
    assert row["state"] == "lost" and row["exit_code"] is None
    assert jobs.jobs(session["id"], active_only=True) == []


def test_detached_status_recovers_after_a_temporary_probe_failure(jobs, session):
    job = jobs.start(session["id"], "sleep 300", pid=4242)

    def unavailable(_job_id):
        raise TimeoutError("sandbox probe timed out")

    sandbox = SimpleNamespace(job_state=unavailable)
    service = JobService(jobs, sandbox, session["id"])
    assert service.status(job["id"])["state"] == "running"
    assert service.jobs(active_only=True)["jobs"][0]["state"] == "running"
    assert jobs.job(job["id"])["exit_code"] is None

    sandbox.job_state = lambda _job_id: {"state": "exited", "exit_code": 7}
    assert service.status(job["id"])["exit_code"] == 7
    assert service.jobs(active_only=True)["jobs"] == []
    assert service.status("missing")["ok"] is False


def test_detached_log_read_failure_preserves_the_retry_cursor(jobs, session):
    job = jobs.start(session["id"], "sleep 300")

    def unavailable(*_args, **_kwargs):
        raise OSError("temporary output transport failure")

    sandbox = SimpleNamespace(job_output=unavailable)
    service = JobService(jobs, sandbox, session["id"])
    assert service.logs(job["id"])["ok"] is False
    assert jobs.job(job["id"])["stdout_offset"] == 0

    reads = []

    def restored(_job_id, _stream, *, offset, limit):
        reads.append(offset)
        return "evidence", offset + len("evidence")

    sandbox.job_output = restored
    assert service.logs(job["id"])["text"] == "evidence"
    assert reads == [0]
    assert jobs.job(job["id"])["stdout_offset"] == len("evidence")
    assert service.logs("missing")["ok"] is False
    assert service.logs(job["id"], stream="invalid")["ok"] is False


def test_detached_cancel_failure_can_retry_without_claiming_success(jobs, session):
    job = jobs.start(session["id"], "sleep 300")

    def unavailable(_job_id):
        raise ConnectionError("sandbox disconnected during cancellation")

    sandbox = SimpleNamespace(job_cancel=unavailable)
    service = JobService(jobs, sandbox, session["id"])
    assert service.cancel(job["id"])["ok"] is False
    assert jobs.job(job["id"])["state"] == "running"

    cancelled = []
    sandbox.job_cancel = cancelled.append
    assert service.cancel(job["id"])["state"] == "cancelled"
    assert cancelled == [job["id"]]
    assert service.cancel(job["id"])["state"] == "cancelled"
    assert cancelled == [job["id"]]
    assert service.cancel("missing")["ok"] is False


def test_job_rows_record_lifecycle_and_keep_the_first_terminal_state(jobs, session):
    job = jobs.start(session["id"], "sleep 300", kind="bash", pid=4242)
    assert job["state"] == "running"
    assert job["exit_code"] is None

    jobs.note_progress(job["id"], stdout_offset=120)
    assert jobs.note_progress(job["id"], stdout_offset=40)["stdout_offset"] == 120

    finished = jobs.finish(job["id"], "exited", exit_code=7)
    assert finished["exit_code"] == 7
    assert finished["finished_at"] is not None
    assert jobs.finish(job["id"], "cancelled")["state"] == "exited"
    assert jobs.finish(job["id"], "cancelled")["exit_code"] == 7

    with pytest.raises(JobConflict):
        jobs.finish(job["id"], "running")


def test_jobs_of_a_vanished_container_are_lost_not_exited(jobs, session):
    one = jobs.start(session["id"], "sleep 300")
    two = jobs.start(session["id"], "sleep 300")
    jobs.finish(two["id"], "exited", exit_code=0)

    lost = jobs.mark_lost_for_session(session["id"])
    assert [row["id"] for row in lost] == [one["id"]]
    assert jobs.job(one["id"])["state"] == "lost"
    assert jobs.job(one["id"])["exit_code"] is None
    assert jobs.job(two["id"])["state"] == "exited"


def test_detached_job_outlives_the_sixty_second_foreground_ceiling(
    jobs, session, tmp_path
):
    """The point of P4a: a command longer than the exec timeout can finish."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("echo phase-1; sleep 1; echo phase-2; exit 3")
    assert started["ok"] is True

    job_id = started["job_id"]
    assert service.status(job_id)["state"] in {"running", "exited"}

    def completed_job():
        view = service.status(job_id)
        return view if view["state"] == "exited" else None

    final = wait_for(completed_job)
    assert final is not None, "detached job never reached a terminal state"
    assert final["exit_code"] == 3

    first = service.logs(job_id)
    assert "phase-1" in first["text"]
    assert service.logs(job_id)["text"] == ""
    assert service.logs(job_id, offset=0)["text"] == first["text"]


def test_job_logs_report_truncation_instead_of_hiding_it(jobs, session, tmp_path):
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("for i in $(seq 1 500); do echo 0123456789; done")
    assert started["ok"] is True
    job_id = started["job_id"]
    assert wait_for(
        lambda: service.status(job_id)["state"] == "exited"
    ), "job never exited"

    page = service.logs(job_id, limit=1000)
    assert page["truncated"] is True
    assert len(page["text"]) == 1000
    assert page["next_offset"] == 1000
    assert service.logs(job_id, limit=1000)["offset"] == 1000


def test_job_start_applies_the_same_host_path_policy_as_bash(jobs, session, tmp_path):
    """A long job must not be a way around the command policy."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])

    blocked = service.start("cat /host/etc/shadow")
    assert blocked["ok"] is False
    assert "blocked" in blocked["error"]
    assert jobs.jobs(session["id"]) == []


def test_cancelling_a_job_kills_it_and_records_the_cancellation(
    jobs, session, tmp_path
):
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("sleep 120")
    job_id = started["job_id"]
    assert started["ok"] is True
    assert service.status(job_id)["state"] == "running"

    assert service.cancel(job_id)["ok"] is True
    assert jobs.job(job_id)["state"] == "cancelled"
    assert jobs.job(job_id)["finished_at"] is not None
    assert service.cancel(job_id)["note"] == "job had already finished"


def test_a_job_whose_process_disappeared_is_reported_as_unknown(
    jobs, session, tmp_path
):
    """The distinction the model depends on: unknown is not success."""
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    job = jobs.start(session["id"], "sleep 300", pid=0)
    directory = sandbox._job_dir(job["id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pid").write_text("2147483646", encoding="utf-8")

    view = service.status(job["id"])
    assert view["state"] == "lost"
    assert view["exit_code"] is None
    assert "not" in view["warning"] and "success" in view["warning"]


def test_jobs_are_scoped_to_their_own_session(jobs, db, session):
    other = AgentSessionStore(db).create_session(
        "competition", "competition:run:other"
    )
    job = jobs.start(other["id"], "sleep 300")
    service = JobService(jobs, LocalSandbox("member", os.getcwd()), session["id"])

    for view in (
        service.status(job["id"]),
        service.logs(job["id"]),
        service.cancel(job["id"]),
    ):
        assert view["ok"] is False
        assert view["error"] == "job belongs to another session"


def test_stopping_a_run_resolves_its_jobs_as_lost_not_exited(db, tmp_path):
    """A container teardown leaves jobs whose fate was never recorded.

    Reporting them as ``exited 0`` would tell a reader the brute force
    finished successfully when it was actually killed with the container.
    """
    from backend.competition.store import CompetitionStore

    store = CompetitionStore(db)
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,created_at,updated_at)
               VALUES ('wf-jobs','test','test','{}','digest',now(),now())"""
        )
    run = store.create_run("wf-jobs", "platform/game/team", "jobs-start", {})
    run = store.transition_run(run["id"], "importing", revision=run["revision"])
    run = store.transition_run(run["id"], "running", revision=run["revision"])
    challenge = store.register_challenge(run["id"], "job-orphan", {})
    assert store.mark_ready(challenge["id"])
    assignment = store.assign(run["id"], challenge["id"], "amber", "worker")

    jobs = AgentJobStore(db)
    running_job = jobs.start(assignment["session_id"], "hashcat -a0 hash list")
    done_job = jobs.start(assignment["session_id"], "echo quick")
    jobs.finish(done_job["id"], "exited", exit_code=0)

    lost = jobs.mark_lost_for_session(assignment["session_id"])
    assert [row["id"] for row in lost] == [running_job["id"]]
    assert jobs.job(running_job["id"])["state"] == "lost"
    assert jobs.job(running_job["id"])["exit_code"] is None
    assert jobs.job(done_job["id"])["state"] == "exited"
    assert jobs.job(done_job["id"])["exit_code"] == 0


def test_health_view_turn_aggregation_tracks_only_the_in_flight_turn(db, session):
    """A finished turn must not look in-flight forever.

    ``current_turn_started_at`` filters on status='running'. Without that
    filter a completed session would report a turn in flight indefinitely and
    its quiet timer would never advance, hiding a real stall.
    """
    store = AgentSessionStore(db)
    store.claim_writer(session["id"], "competition-assignment", "w1")

    store.start_turn(session["id"], 1, "w1", 1)
    store.finish_turn(session["id"], 1, "completed", usage={"prompt_tokens": 10})
    first = store.health(session["id"])
    assert first["current_turn"] == 1
    assert first["turn_status"] == "completed"
    assert first["current_turn_started_at"] is None

    store.start_turn(session["id"], 2, "w1", 1)
    live = store.health(session["id"])
    assert live["current_turn"] == 2
    assert live["turn_status"] == "running"
    assert live["current_turn_started_at"] is not None

    store.finish_turn(session["id"], 2, "completed")
    assert store.health(session["id"])["current_turn_started_at"] is None


def test_a_long_in_flight_turn_does_not_accumulate_quiet_time(db, session):
    """The case that must not be misread: a slow provider turn is progress.

    A provider read timeout alone is 300s. If quiet time grew while a turn was
    in flight, the watchdog would cancel work that was simply slow.
    """
    from backend.agent.watchdog import classify

    store = AgentSessionStore(db)
    store.claim_writer(session["id"], "competition-assignment", "w1")
    with db.connect() as connection:
        connection.execute(
            "UPDATE agent_sessions SET created_at=now()-interval '1 hour' WHERE id=%s",
            (session["id"],),
        )
        connection.execute(
            """UPDATE agent_session_writers
               SET last_renewed_at=now()-interval '1 hour' WHERE session_id=%s""",
            (session["id"],),
        )
    store.start_turn(session["id"], 1, "w1", 1)

    fresh = store.health(session["id"])
    assert fresh["quiet_seconds"] < 60
    assert classify(fresh).level == "ok"

    with db.connect() as connection:
        connection.execute(
            """UPDATE agent_turns SET started_at=now()-interval '1 hour'
               WHERE session_id=%s""",
            (session["id"],),
        )
    wedged = store.health(session["id"])
    assert wedged["quiet_seconds"] > 3000
    assert classify(wedged).level == "handoff"


def test_incremental_log_reads_tile_the_output_exactly(jobs, session, tmp_path):
    """Repeated polls must neither lose bytes nor repeat them.

    A model polling a long job reasons from the concatenation of what it read.
    A gap silently drops evidence; an overlap makes it count the same finding
    twice. An explicit offset is a seek, so it must not disturb the cursor the
    default path uses.
    """
    sandbox = LocalSandbox("member", tmp_path)
    service = JobService(jobs, sandbox, session["id"])
    started = service.start("for i in $(seq 1 200); do echo LINE$i; done")
    job_id = started["job_id"]
    assert wait_for(lambda: service.status(job_id)["state"] == "exited")

    chunks = []
    while True:
        page = service.logs(job_id, limit=300)
        if not page["text"]:
            break
        chunks.append(page["text"])
    written = (sandbox._job_dir(job_id) / "stdout").read_bytes().decode()
    assert len(chunks) > 1, "limit was too large to exercise paging"
    assert "".join(chunks) == written

    before = jobs.job(job_id)["stdout_offset"]
    service.logs(job_id, offset=0, limit=50)
    assert jobs.job(job_id)["stdout_offset"] == before


def test_sweep_stale_closes_only_long_silent_running_jobs(jobs, session, db):
    """T5: a periodic sweep closes jobs idle past the threshold as lost.

    A job whose process vanished without an exit status leaves a permanently
    ``running`` row that no poll will ever close.  The time-based sweep closes
    it; a job that is merely slow but still reporting progress is left alone.
    """
    stale = jobs.start(session["id"], "sleep 99999")
    fresh = jobs.start(session["id"], "sleep 1")
    done = jobs.start(session["id"], "echo hi")
    jobs.finish(done["id"], "exited", exit_code=0)

    with db.connect() as connection:
        connection.execute(
            "UPDATE agent_jobs SET last_progress_at=now()-interval '2 hours' WHERE id=%s",
            (stale["id"],),
        )

    swept = jobs.sweep_stale(idle_seconds=1800)
    assert [row["id"] for row in swept] == [stale["id"]]
    assert jobs.job(stale["id"])["state"] == "lost"
    assert jobs.job(stale["id"])["exit_code"] is None
    assert jobs.job(fresh["id"])["state"] == "running"
    assert jobs.job(done["id"])["state"] == "exited"

    assert jobs.sweep_stale(idle_seconds=1800) == []


def test_sweep_stale_rejects_a_nonpositive_threshold(jobs):
    with pytest.raises(ValueError):
        jobs.sweep_stale(idle_seconds=0)
