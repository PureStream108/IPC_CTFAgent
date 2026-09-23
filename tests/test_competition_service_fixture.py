"""A database-backed competition coordinator acceptance flow.

The platform is deterministic, but the run, assignments, leases, submissions,
and writeups all use the production PostgreSQL paths.  This catches regressions
that isolated store tests cannot see, especially around the coordinator's
ordering of platform calls and durable state transitions.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from backend.blackboard import graph_store
from backend.competition.fixture import FixturePlatform
from backend.competition.service import CompetitionService
from backend.ops.models import PlatformWorkflowSpec
from backend.platform.mapping import PlatformChallenge


pytestmark = pytest.mark.postgres


def _spec() -> PlatformWorkflowSpec:
    return PlatformWorkflowSpec.model_validate(
        {
            "name": "Fixture service acceptance",
            "competition_id": "fixture-game",
            "team_id": "fixture-team",
            "remote_instance_limit": 1,
            "challenges": {
                "list_url": "https://fixture.invalid/challenges",
            },
            "submit": {
                "url": "https://fixture.invalid/submit/{{external_id}}",
                "json_template": {"flag": "{{flag}}"},
                "success_statuses": [200],
                "success_path": "correct",
                "success_values": [True],
                "wrong_values": [False],
                "pending_values": ["pending"],
                "query_url": "https://fixture.invalid/query/{{submission_id}}",
            },
        }
    )


class _WorkflowStore:
    def __init__(self, workflow: dict):
        self.workflow = workflow

    def get_workflow(self, workflow_id: str) -> dict:
        assert workflow_id == self.workflow["id"]
        return self.workflow


class _FixtureOps:
    def __init__(self, db, workflow: dict):
        self.db = db
        self.store = _WorkflowStore(workflow)
        self.projects: dict[str, str] = {}

    def _import(self, workflow_id: str, spec: PlatformWorkflowSpec, select: list[str]):
        del spec
        imported = []
        with self.db.connect() as connection:
            for external_id in select:
                project_id = self.projects.get(external_id)
                if project_id is None:
                    project_id = graph_store.create_project(
                        connection,
                        f"Fixture {external_id}",
                        "fixture://challenge",
                        "capture the flag",
                        "web" if external_id == "remote" else "misc",
                        external_id=external_id,
                        platform="fixture",
                    )
                    # The coordinator's verified-flag path intentionally
                    # requires a completion edge.  A real Member would create
                    # this edge before submitting; the fixture creates the
                    # equivalent durable fact explicitly.
                    connection.execute(
                        """INSERT INTO intents
                           (id,project_id,to_fact_id,description,creator,created_at)
                           VALUES (%s,%s,'goal','fixture completion','fixture',now())""",
                        ("fixture-goal", project_id),
                    )
                    connection.execute(
                        """INSERT INTO workflow_challenges (workflow_id,external_id,project_id)
                           VALUES (%s,%s,%s)""",
                        (workflow_id, external_id, project_id),
                    )
                    self.projects[external_id] = project_id
                imported.append({
                    "external_id": external_id,
                    "project_id": project_id,
                    "created": True,
                })
        return {"operation": "import", "imported": imported}


class _FixtureConfig:
    member = None
    limits = SimpleNamespace(max_concurrent_tasks=10)

    @staticmethod
    def startup_errors() -> list[str]:
        return []

    @staticmethod
    def available_members() -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in ("amber", "agate", "topaz", "pearl")]


class _FixtureLogger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def read_log(self, _kind: str, _project_id: str, *, limit: int = 160):
        del limit
        return []

    def project(self, event: str, _project_id: str, **fields):
        self.events.append((event, fields))


class _FixtureOrchestrator:
    def __init__(self, service: CompetitionService):
        self.service = service
        self.owners: dict[str, str] = {}
        self.started: list[str] = []
        self.stopped: list[str] = []

    def _refresh_owners(self) -> None:
        self.owners.clear()
        for assignment in self.service.store.assignments(
            self.service.store.active_run()["id"], active_only=True
        ):
            challenge = next(
                item for item in self.service.store.challenges(assignment["run_id"])
                if item["id"] == assignment["challenge_id"]
            )
            if challenge.get("project_id"):
                self.owners[assignment["member"]] = challenge["project_id"]

    def active_member_owners(self) -> dict[str, str]:
        return dict(self.owners)

    def start_project_async(self, project_id: str) -> dict[str, str]:
        self.started.append(project_id)
        self._refresh_owners()
        return {"project_id": project_id, "status": "queued"}

    def stop_project(self, project_id: str) -> None:
        self.stopped.append(project_id)
        self.owners = {
            member: value for member, value in self.owners.items() if value != project_id
        }

    def _resume_after_verdict(self, *_args, **_kwargs) -> None:
        return None


def test_competition_service_fixture_runs_to_finished_with_wp_and_release(tmp_path):
    # The autouse PostgreSQL fixture exposes the configured database through
    # the normal runtime environment; importing here avoids constructing an
    # application (and its background workers) for this focused acceptance.
    from backend.persistence.database import Database

    db = Database().configure()
    logger = _FixtureLogger()
    state = SimpleNamespace(
        db=db,
        instance_id="fixture-coordinator",
        artifact_root=tmp_path / "artifacts",
        wp_dir=tmp_path / "artifacts" / "writeups",
        config=_FixtureConfig(),
        logger=logger,
        orchestrator=None,
    )
    state.artifact_root.mkdir(parents=True, exist_ok=True)
    state.wp_dir.mkdir(parents=True, exist_ok=True)

    spec = _spec()
    workflow_id = "fixture-service-workflow"
    digest = hashlib.sha256(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,status,confirmed_digest,created_at,updated_at)
               VALUES (%s,'fixture',%s,%s,%s,'confirmed',%s,now(),now())""",
            (workflow_id, spec.name, Jsonb(spec.model_dump(mode="json")), digest, digest),
        )

    platform = FixturePlatform(
        [
            # Web remote is intentionally first in scheduler priority.
            PlatformChallenge(
                external_id="remote", title="Remote", category="web",
                description="remote fixture", remote=True,
            ),
            PlatformChallenge(
                external_id="local", title="Local", category="misc",
                description="local fixture",
            ),
        ],
        flags={"remote": "flag{remote}", "local": "flag{local}"},
        remote_limit=1,
    )
    ops = _FixtureOps(db, {
        "id": workflow_id,
        "status": "confirmed",
        "confirmed_digest": digest,
        "spec_digest": digest,
        "spec": spec,
    })
    service = CompetitionService(
        state,
        tick_interval=0.01,
        platform_factory=lambda _workflow_id, _spec: platform,
        ops_service=ops,
    )
    orchestrator = _FixtureOrchestrator(service)
    state.orchestrator = orchestrator

    try:
        snapshot = service.start(workflow_id, "fixture-run")
        run_id = snapshot["run"]["id"]

        # First tick prepares the remote instance and reserves both primary
        # seats.  The next tick reconciles the asynchronous project owners and
        # persists a helper seat for the two-person collaboration path.
        service.tick()
        service.tick()
        assignments = service.store.assignments(run_id, active_only=True)
        assert {item["role"] for item in assignments} >= {"primary", "helper"}
        assert len(platform.instances()) == 1

        by_external = {
            item["external_id"]: item for item in service.store.challenges(run_id)
        }
        primary = {
            item["challenge_id"]: item
            for item in assignments
            if item["role"] == "primary"
        }

        # Wrong verdicts are persisted and apply a cooldown.  Move the clock
        # forward in the database before the next candidate to keep the test
        # deterministic and fast.
        local = by_external["local"]
        local_assignment = primary[local["id"]]
        service.submit_candidate(
            run_id, local["id"], local_assignment["session_id"],
            "flag{wrong}", "fixture wrong evidence", local["instance_generation"],
        )
        service.tick()
        with db.connect() as connection:
            wrong = connection.execute(
                "SELECT status FROM competition_submissions WHERE challenge_id=%s ORDER BY created_at DESC LIMIT 1",
                (local["id"],),
            ).fetchone()
            assert wrong["status"] == "wrong"
            connection.execute(
                "UPDATE competition_challenges SET next_submission_at=now()-interval '1 second' WHERE id=%s",
                (local["id"],),
            )

        # The remote challenge exercises asynchronous pending -> query ->
        # correct reconciliation, including instance release and WP creation.
        remote = by_external["remote"]
        remote_assignment = primary[remote["id"]]
        service.submit_candidate(
            run_id, remote["id"], remote_assignment["session_id"],
            "fixture:pending:correct", "fixture pending evidence", remote["instance_generation"],
        )
        service.tick()
        with db.connect() as connection:
            pending = connection.execute(
                "SELECT status,platform_submission_id FROM competition_submissions WHERE challenge_id=%s ORDER BY created_at DESC LIMIT 1",
                (remote["id"],),
            ).fetchone()
            assert pending["status"] == "pending"
            assert pending["platform_submission_id"]

        service.submit_candidate(
            run_id, local["id"], local_assignment["session_id"],
            "flag{local}", "fixture correct evidence", local["instance_generation"],
        )
        service.tick()
        service.tick()  # resolves the remote pending submission

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            service.tick()
            if service.store.get_run(run_id)["status"] == "finished":
                break
            time.sleep(0.02)

        assert service.store.get_run(run_id)["status"] == "finished"
        challenges = {item["external_id"]: item for item in service.store.challenges(run_id)}
        assert {item["state"] for item in challenges.values()} == {"solved"}
        assert service.store.assignments(run_id, active_only=True) == []
        with db.connect() as connection:
            jobs = connection.execute(
                "SELECT status,artifact_path FROM competition_wp_jobs WHERE run_id=%s ORDER BY challenge_id",
                (run_id,),
            ).fetchall()
            assert len(jobs) == 2
            assert all(job["status"] == "done" for job in jobs)
            assert all(job["artifact_path"] for job in jobs)
        assert platform.instances() == []
        assert set(orchestrator.stopped) == set(ops.projects.values())
    finally:
        service.shutdown()
        db.close()


def _new_fixture_service(tmp_path: Path, platform: FixturePlatform, suffix: str):
    """Build the production coordinator around a deterministic platform."""
    from backend.persistence.database import Database

    db = Database().configure()
    spec = _spec()
    workflow_id = f"fixture-lifecycle-{suffix}"
    digest = hashlib.sha256(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,status,confirmed_digest,created_at,updated_at)
               VALUES (%s,'fixture',%s,%s,%s,'confirmed',%s,now(),now())""",
            (workflow_id, spec.name, Jsonb(spec.model_dump(mode="json")), digest, digest),
        )
    state = SimpleNamespace(
        db=db,
        instance_id=f"lifecycle-{suffix}",
        artifact_root=tmp_path / suffix / "artifacts",
        wp_dir=tmp_path / suffix / "artifacts" / "writeups",
        config=_FixtureConfig(),
        logger=_FixtureLogger(),
        orchestrator=None,
    )
    state.artifact_root.mkdir(parents=True, exist_ok=True)
    state.wp_dir.mkdir(parents=True, exist_ok=True)
    ops = _FixtureOps(db, {
        "id": workflow_id,
        "status": "confirmed",
        "confirmed_digest": digest,
        "spec_digest": digest,
        "spec": spec,
    })
    service = CompetitionService(
        state,
        tick_interval=0.01,
        platform_factory=lambda _workflow_id, _spec: platform,
        ops_service=ops,
    )
    state.orchestrator = _FixtureOrchestrator(service)
    return db, service, state, ops, workflow_id


def test_paused_run_reconciles_pending_submission_and_renews_without_new_submit(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="paused-remote", title="Paused remote", category="web",
            description="pause fixture", remote=True,
        )],
        flags={"paused-remote": "fixture:correct"},
        remote_limit=1,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "pause"
    )
    try:
        snapshot = service.start(workflow_id, "pause-run")
        run_id = snapshot["run"]["id"]
        service.tick()
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        assignment = service.store.assignments(run_id, active_only=True)[0]
        service.submit_candidate(
            run_id, challenge["id"], assignment["session_id"],
            "fixture:pending:unknown", "pending evidence", challenge["instance_generation"],
        )
        service.tick()
        pending = db_row = None
        with db.connect() as connection:
            db_row = connection.execute(
                """SELECT id,status,platform_submission_id FROM competition_submissions
                   WHERE challenge_id=%s ORDER BY created_at DESC LIMIT 1""",
                (challenge["id"],),
            ).fetchone()
            connection.execute(
                """UPDATE competition_instances SET next_renew_at=now()-interval '1 second'
                   WHERE challenge_id=%s""",
                (challenge["id"],),
            )
        pending = db_row
        assert pending["status"] == "pending"
        assert pending["platform_submission_id"]
        deadline_before = challenge["deadline_at"]

        paused = service.control(
            run_id, "pause", service.store.get_run(run_id)["revision"]
        )
        assert paused["run"]["status"] == "paused"
        calls_before = list(platform.calls)
        service.tick()

        # Pause still performs the two operations that are safe and required:
        # renew the owned target and reconcile the already accepted request.
        assert platform.renew_calls == ["paused-remote"]
        assert platform.query_calls == [("paused-remote", pending["platform_submission_id"])]
        assert platform.calls == calls_before
        assert service.store.get_run(run_id)["status"] == "paused"
        assert service.store.challenges(run_id)[0]["deadline_at"] == deadline_before

        # A candidate queued while paused remains local.  It is sent only after
        # an explicit Resume, never as a side effect of reconciliation.
        service.store.queue_recovered_candidate(
            run_id, challenge["id"], "fixture:correct", "resume evidence"
        )
        service.tick()
        assert platform.calls == calls_before
        with db.connect() as connection:
            queued = connection.execute(
                "SELECT status FROM competition_submissions WHERE challenge_id=%s AND candidate=%s",
                (challenge["id"], "fixture:correct"),
            ).fetchone()
        assert queued["status"] == "queued"

        service.control(run_id, "resume", service.store.get_run(run_id)["revision"])
        for _ in range(20):
            service.tick()
            if service.store.get_run(run_id)["status"] == "finished":
                break
            time.sleep(0.01)
        assert ("paused-remote", "fixture:correct") in platform.calls
        assert service.store.get_run(run_id)["status"] == "finished"
    finally:
        service.shutdown()
        db.close()


def test_instance_renewal_failure_rebuilds_and_fences_new_generation(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="rebuild-remote", title="Rebuild remote", category="web",
            description="rebuild fixture", remote=True,
        )],
        flags={"rebuild-remote": "fixture:correct"},
        remote_limit=1,
        renew_failures=1,
        rebuild_on_renew_failure=True,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "rebuild"
    )
    try:
        snapshot = service.start(workflow_id, "rebuild-run")
        run_id = snapshot["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_instances SET next_renew_at=now()-interval '1 second' WHERE challenge_id=%s",
                (challenge["id"],),
            )
        service.tick()
        current = service.store.challenges(run_id)[0]
        assert current["instance_generation"] == challenge["instance_generation"] + 1
        assert platform.rebuilds == ["rebuild-remote"]
        assert any(
            kind == "instance.rebuilt"
            for kind, _fields in _state.logger.events
        )
        assert platform.instances()[0]["generation"] == 2
    finally:
        service.shutdown()
        db.close()


def test_fixture_repeated_runs_release_assignments_instances_and_wp(tmp_path):
    """Run several controlled matches back-to-back without durable leaks."""
    for round_no in range(3):
        external_id = f"repeat-{round_no}"
        platform = FixturePlatform(
            [PlatformChallenge(
                external_id=external_id,
                title=f"Repeated {round_no}",
                category="web",
                description="repeated lifecycle fixture",
                remote=True,
            )],
            flags={external_id: "fixture:correct"},
            remote_limit=1,
        )
        db, service, _state, _ops, workflow_id = _new_fixture_service(
            tmp_path, platform, f"repeat-{round_no}"
        )
        try:
            run_id = service.start(workflow_id, f"repeat-run-{round_no}")["run"]["id"]
            for _ in range(3):
                service.tick()
            challenge = service.store.challenges(run_id)[0]
            assignment = service.store.assignments(run_id, active_only=True)[0]
            service.submit_candidate(
                run_id,
                challenge["id"],
                assignment["session_id"],
                "fixture:correct",
                "repeated fixture evidence",
                challenge["instance_generation"],
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                service.tick()
                if service.store.get_run(run_id)["status"] == "finished":
                    break
                time.sleep(0.01)

            assert service.store.get_run(run_id)["status"] == "finished"
            assert service.store.assignments(run_id, active_only=True) == []
            assert platform.instances() == []
            with db.connect() as connection:
                submission = connection.execute(
                    """SELECT status FROM competition_submissions
                       WHERE challenge_id=%s ORDER BY created_at DESC LIMIT 1""",
                    (challenge["id"],),
                ).fetchone()
                wp = connection.execute(
                    """SELECT status FROM competition_wp_jobs
                       WHERE run_id=%s AND challenge_id=%s""",
                    (run_id, challenge["id"]),
                ).fetchone()
            assert submission["status"] == "correct"
            assert wp["status"] == "done"
        finally:
            service.shutdown()
            db.close()


def test_late_correct_verdict_wins_end_state_race_and_creates_one_wp(tmp_path):
    """A verdict accepted before shutdown remains authoritative after draining."""
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="late-race", title="Late race", category="misc",
            description="late verdict fixture",
        )],
        flags={"late-race": "fixture:correct"},
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "late-race"
    )
    try:
        run = service.start(workflow_id, "late-race-run")
        run_id = run["run"]["id"]
        service.tick()
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        assignment = service.store.assignments(run_id, active_only=True)[0]
        queued = service.submit_candidate(
            run_id, challenge["id"], assignment["session_id"],
            "fixture:correct", "race evidence", challenge["instance_generation"],
        )
        claimed = service.store.claim_submission(run_id)
        assert claimed["id"] == queued["id"]

        # Simulate the coordinator entering drain after the request was sent,
        # followed by the platform's correct response arriving late.
        service.store.release(
            assignment["id"], assignment["lease_owner"], assignment["epoch"]
        )
        current = service.store.get_run(run_id)
        service.store.transition_run(
            run_id, "draining", revision=current["revision"]
        )
        finished = service.store.finish_submission(
            claimed["id"], {"verdict": "correct", "submission_id": "late-1"}
        )
        assert finished["status"] == "correct"
        service._on_correct(
            service.store.get_run(run_id), challenge, "fixture:correct"
        )
        # A delayed wrong callback is idempotently ignored by the terminal row.
        assert service.store.finish_submission(
            claimed["id"], {"verdict": "wrong"}
        )["status"] == "correct"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            service.tick()
            if service.store.get_run(run_id)["status"] == "finished":
                break
            time.sleep(0.01)
        assert service.store.get_run(run_id)["status"] == "finished"
        with db.connect() as connection:
            jobs = connection.execute(
                "SELECT status FROM competition_wp_jobs WHERE run_id=%s AND challenge_id=%s",
                (run_id, challenge["id"]),
            ).fetchall()
        assert jobs == [{"status": "done"}]
    finally:
        service.shutdown()
        db.close()


def test_wp_dispatch_caps_concurrency_under_archive_pressure(tmp_path, monkeypatch):
    """Four solved challenges never consume more than two WP seats at once."""
    challenges = [
        PlatformChallenge(
            external_id=f"wp-pressure-{index}", title=f"WP {index}",
            category="misc", description="writeup pressure fixture",
        )
        for index in range(4)
    ]
    platform = FixturePlatform(
        challenges,
        flags={item.external_id: f"fixture:{item.external_id}" for item in challenges},
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "wp-pressure"
    )
    gate = threading.Event()
    counters = {"active": 0, "maximum": 0}
    counter_lock = threading.Lock()
    started: list[str] = []

    def slow_wp(claimed):
        challenge_id = claimed["job"]["challenge_id"]
        with counter_lock:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])
            started.append(challenge_id)
        try:
            gate.wait(3)
            service.store.finish_wp_job(
                claimed["assignment"],
                artifact_path=f"fixture/{challenge_id}.md",
            )
        finally:
            with counter_lock:
                counters["active"] -= 1

    monkeypatch.setattr(service, "_run_wp_job", slow_wp)
    try:
        run_id = service.start(workflow_id, "wp-pressure-run")["run"]["id"]
        service.tick()
        service.tick()
        rows = service.store.challenges(run_id)
        assert len(rows) == 4
        for challenge in rows:
            assignment = next(
                item for item in service.store.assignments(run_id, active_only=True)
                if item["challenge_id"] == challenge["id"]
            )
            service.submit_candidate(
                run_id, challenge["id"], assignment["session_id"],
                f"fixture:{challenge['external_id']}", "pressure evidence",
                challenge["instance_generation"],
            )
            claimed = service.store.claim_submission(run_id)
            service.store.finish_submission(
                claimed["id"], {"verdict": "correct", "submission_id": claimed["id"]}
            )
            service._on_correct(
                service.store.get_run(run_id), challenge,
                f"fixture:{challenge['external_id']}",
            )

        service._dispatch_wp(service.store.get_run(run_id))
        deadline = time.monotonic() + 2
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(started) == 2
        assert counters["maximum"] <= 2
        assert len(service._wp_futures) == 2

        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            service._dispatch_wp(service.store.get_run(run_id))
            with db.connect() as connection:
                jobs = connection.execute(
                    "SELECT status FROM competition_wp_jobs WHERE run_id=%s", (run_id,)
                ).fetchall()
            if len(started) == 4 and len(jobs) == 4 and all(
                item["status"] == "done" for item in jobs
            ):
                break
            time.sleep(0.01)
        assert len(started) == 4
        assert counters["maximum"] <= 2
    finally:
        gate.set()
        service.shutdown()
        db.close()
