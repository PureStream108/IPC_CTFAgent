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


def test_partial_correct_keeps_solver_and_survives_closeout_recovery(tmp_path, monkeypatch):
    platform = FixturePlatform([
        PlatformChallenge(external_id="multi-flag", title="Multi flag", category="pentest", description="Two objectives"),
    ], flags={"multi-flag": "fixture-two"})
    def submit(external_id, flag):
        platform.calls.append((external_id, flag))
        final = flag == "fixture-two"
        return {"verdict": "correct", "challenge_complete": final, "correct_flag_count": 2 if final else 1,
                "total_flag_count": 2, "matched_flag_index": 1 if final else 0}
    monkeypatch.setattr(platform, "submit", submit)
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "multi-flag")
    try:
        run_id = service.start(workflow_id, "multi-flag-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        seat = service.store.assignments(run_id, active_only=True)[0]
        service.submit_candidate(run_id, challenge["id"], seat["session_id"], "fixture-one", "first objective evidence", 0)
        service.tick()
        service.tick()
        current = service.store.challenges(run_id)[0]
        assert current["state"] != "solved"
        assert current["metadata"]["flag_progress"]["correct_flag_count"] == 1
        assert service.store.assignments(run_id, active_only=True)
        with db.connect() as connection:
            assert graph_store.get_project_row(connection, current["project_id"])["status"] != "solved"
        service.submit_candidate(run_id, challenge["id"], seat["session_id"], "fixture-two", "second objective evidence", 0)
        service.tick()
        assert service.store.challenges(run_id)[0]["state"] == "solved"
        assert platform.calls == [("multi-flag", "fixture-one"), ("multi-flag", "fixture-two")]
    finally:
        service.shutdown()
        db.close()


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

    def startup_in_progress(self, _project_id: str) -> bool:
        return False

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


@pytest.mark.parametrize("failure_stage", ["verification", "writeup"])
def test_correct_closeout_recovers_without_resubmitting_or_releasing_early(tmp_path, monkeypatch, failure_stage):
    import backend.competition.service as service_module

    platform = FixturePlatform([
        PlatformChallenge(external_id="closeout", title="Closeout", category="misc", description="fixture"),
    ], flags={"closeout": "fixture:correct"})
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "closeout-fault")
    try:
        run_id = service.start(workflow_id, "closeout-fault-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        seat = service.store.assignments(run_id, active_only=True)[0]
        with db.connect() as connection:
            connection.execute("DELETE FROM intents WHERE project_id=%s AND to_fact_id='goal'", (challenge["project_id"],))

        original_accept = service_module.accept_verified_flag
        original_closeout = service.store.finalize_correct_challenge

        def unavailable(*_args, **_kwargs):
            raise OSError("temporary closeout storage failure")

        def unavailable_writeup(connection, owner_run_id, challenge_id):
            original_closeout(connection, owner_run_id, challenge_id)
            raise OSError("writeup persistence failed after flag verification")

        if failure_stage == "verification":
            monkeypatch.setattr(service_module, "accept_verified_flag", unavailable)
        else:
            monkeypatch.setattr(service.store, "finalize_correct_challenge", unavailable_writeup)
        service.submit_candidate(run_id, challenge["id"], seat["session_id"], "fixture:correct", "verified evidence", 0)
        service.tick()
        assert service.store.get_run(run_id)["status"] == "blocked"
        assert service.store.assignments(run_id, active_only=True)
        with db.connect() as connection:
            submission = connection.execute("SELECT status FROM competition_submissions WHERE run_id=%s", (run_id,)).fetchone()
            assert submission["status"] == "correct"
            assert connection.execute("SELECT 1 FROM competition_wp_jobs WHERE run_id=%s", (run_id,)).fetchone() is None
            assert connection.execute("SELECT flag FROM projects WHERE id=%s", (challenge["project_id"],)).fetchone()["flag"] is None

        monkeypatch.setattr(service_module, "accept_verified_flag", original_accept)
        monkeypatch.setattr(service.store, "finalize_correct_challenge", original_closeout)
        service._process_submissions(service.store.get_run(run_id), allow_new=False)
        assert platform.calls == [("closeout", "fixture:correct")]
        assert all(row["role"] == "wp" for row in service.store.assignments(run_id, active_only=True))
        with db.connect() as connection:
            project = connection.execute("SELECT status,flag FROM projects WHERE id=%s", (challenge["project_id"],)).fetchone()
            assert project["status"] == "solved" and project["flag"] == "fixture:correct"
            assert connection.execute("SELECT count(*) AS n FROM competition_wp_jobs WHERE run_id=%s", (run_id,)).fetchone()["n"] == 1
        service._process_submissions(service.store.get_run(run_id), allow_new=False)
        assert platform.calls == [("closeout", "fixture:correct")]
    finally:
        service.shutdown()
        db.close()


def test_external_solve_refresh_releases_solver_and_instance_once(tmp_path, monkeypatch):
    item = PlatformChallenge(external_id="external", title="External", category="web", description="fixture", remote=True)
    platform = FixturePlatform([item], remote_limit=1)
    db, service, state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "external-solve")
    try:
        run_id = service.start(workflow_id, "external-solve-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        assert service.store.assignments(run_id, active_only=True)
        assert platform.instances()
        stopped = []
        original_stop = platform.stop_instance

        def stop(external_id):
            stopped.append(external_id)
            original_stop(external_id)

        monkeypatch.setattr(platform, "stop_instance", stop)
        platform.replace_challenges([item.model_copy(update={"solved": True})])
        service.control(run_id, "refresh", service.store.get_run(run_id)["revision"])
        assert service.store.challenges(run_id)[0]["state"] == "solved"
        assert service.store.assignments(run_id, active_only=True) == []
        assert platform.instances() == []
        assert challenge["project_id"] in state.orchestrator.stopped
        service.control(run_id, "refresh", service.store.get_run(run_id)["revision"])
        assert stopped == ["external"]
        with db.connect() as connection:
            assert connection.execute("SELECT flag FROM projects WHERE id=%s", (challenge["project_id"],)).fetchone()["flag"] is None
            assert connection.execute("SELECT 1 FROM competition_wp_jobs WHERE run_id=%s", (run_id,)).fetchone() is None
    finally:
        service.shutdown()
        db.close()


def test_competition_service_fixture_runs_to_finished_with_wp_and_release(tmp_path):
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
        service.tick()

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

        assert platform.renew_calls == ["paused-remote"]
        assert platform.query_calls == [("paused-remote", pending["platform_submission_id"])]
        assert platform.calls == calls_before
        assert service.store.get_run(run_id)["status"] == "paused"
        assert service.store.challenges(run_id)[0]["deadline_at"] == deadline_before

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


def test_transient_instance_renewal_failure_preserves_target_and_generation(tmp_path):
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
        assert current["instance_generation"] == challenge["instance_generation"]
        assert platform.rebuilds == []
        assert platform.instances()[0]["generation"] == 1
        events = service.store.run_events(run_id)
        assert any(event["kind"] == "instance.renew_failed" for event in events)
        assert not any(event["kind"] == "instance.rebuild_requested" for event in events)
    finally:
        service.shutdown()
        db.close()


def test_instance_entry_is_published_once_to_active_members(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="entry-remote", title="Entry", category="web",
            description="entry fixture", remote=True,
        )],
        flags={"entry-remote": "fixture:correct"}, remote_limit=1,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "entry"
    )
    try:
        run_id = service.start(workflow_id, "entry-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        snapshot = service.store.run_snapshot(run_id)
        assert snapshot["resource_capacity"]["remote"]["limit"] == 1
        assert snapshot["resource_capacity"]["remote"]["used"] == 1
        assert snapshot["resource_capacity"]["local"]["used"] == 0
        assert snapshot["resource_capacity"]["remote"]["source"] == "workflow.remote_instance_limit"
        kinds = {row["kind"] for row in service.store.run_events(run_id)}
        assert {"instance.start_requested", "instance.started"}.issubset(kinds)
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_instances SET metadata=%s WHERE challenge_id=%s",
                (Jsonb({"entry": "10.0.0.5:30001"}), challenge["id"]),
            )
        messages = []
        service.publish_recon = messages.append
        run = service.store.get_run(run_id)
        service._publish_instance_entries(run)
        service._publish_instance_entries(run)
        assert len(messages) == 1
        assert messages[0].payload["kind"] == "target_instance"
        assert messages[0].payload["entry"] == "10.0.0.5:30001"
    finally:
        service.shutdown()
        db.close()


def test_missing_instance_renewal_is_marked_expired_for_deferred_repair(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="expired-remote", title="Expired", category="web",
            description="expired fixture", remote=True,
        )],
        flags={"expired-remote": "fixture:correct"}, remote_limit=1,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "expired"
    )
    try:
        run_id = service.start(workflow_id, "expired-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        platform._instances.clear()
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_instances SET next_renew_at=now()-interval '1 second' WHERE challenge_id=%s",
                (challenge["id"],),
            )
        service._renew_instances(service.store.get_run(run_id))
        with db.connect() as connection:
            row = connection.execute(
                "SELECT state,metadata->>'repair_required' AS repair_required FROM competition_instances WHERE challenge_id=%s",
                (challenge["id"],),
            ).fetchone()
        assert row["state"] == "expired"
        assert row["repair_required"] == "true"
        assert any(event["kind"] == "instance.expired" for event in service.store.run_events(run_id))
        run = service.store.get_run(run_id)
        service._repair_assigned_instances(run)
        assert platform.instances() == []
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_instances SET metadata=metadata || %s WHERE challenge_id=%s",
                (Jsonb({"repair_after": "2026-01-01T00:00:00+00:00"}), challenge["id"]),
            )
        service._repair_assigned_instances(run)
        assert platform.instances()
        service._publish_instance_entries(run)
        assert any(event["kind"] == "instance.repaired" for event in service.store.run_events(run_id))
    finally:
        service.shutdown()
        db.close()


def test_submission_wrong_verdict_keeps_run_retriable(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="wrong-remote", title="Wrong", category="web",
            description="wrong fixture", remote=False,
        )],
        flags={"wrong-remote": "fixture:correct"}, remote_limit=0,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "wrong")
    try:
        run_id = service.start(workflow_id, "wrong-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        seat = service.store.assignments(run_id, active_only=True)[0]
        service.submit_candidate(run_id, challenge["id"], seat["session_id"], "fixture:wrong", "wrong evidence", challenge["instance_generation"])
        service.tick()
        assert platform.calls[-1] == ("wrong-remote", "fixture:wrong")
        assert service.store.get_run(run_id)["status"] == "running"
        assert service.store.challenges(run_id)[0]["state"] == "solving"
    finally:
        service.shutdown()
        db.close()


def test_rate_limited_verdict_is_requeued_with_backoff(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="rate-remote", title="Rate", category="web",
            description="rate fixture", remote=False,
        )],
        flags={"rate-remote": "fixture:correct"}, remote_limit=0,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "rate")
    try:
        run_id = service.start(workflow_id, "rate-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        seat = service.store.assignments(run_id, active_only=True)[0]
        service.submit_candidate(run_id, challenge["id"], seat["session_id"], "fixture:rate-limited", "rate evidence", challenge["instance_generation"])
        service.tick()
        with db.connect() as connection:
            row = connection.execute(
                "SELECT status FROM competition_submissions WHERE challenge_id=%s ORDER BY created_at DESC LIMIT 1",
                (challenge["id"],),
            ).fetchone()
        assert row["status"] == "queued"
    finally:
        service.shutdown()
        db.close()


def test_renewal_failure_without_rebuild_uses_delayed_retry(tmp_path):
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="retry-remote", title="Retry", category="web",
            description="retry fixture", remote=True,
        )],
        flags={"retry-remote": "fixture:correct"}, remote_limit=1,
        renew_failures=1, rebuild_on_renew_failure=False,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(tmp_path, platform, "retry")
    try:
        run_id = service.start(workflow_id, "retry-run")["run"]["id"]
        service.tick()
        challenge = service.store.challenges(run_id)[0]
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_instances SET next_renew_at=now()-interval '1 second' WHERE challenge_id=%s",
                (challenge["id"],),
            )
        service._renew_instances(service.store.get_run(run_id))
        with db.connect() as connection:
            row = connection.execute(
                "SELECT state,next_renew_at>now() AS delayed FROM competition_instances WHERE challenge_id=%s",
                (challenge["id"],),
            ).fetchone()
        assert row["state"] == "ready" and row["delayed"] is True
        assert any(event["kind"] == "instance.renew_failed" for event in service.store.run_events(run_id))
    finally:
        service.shutdown()
        db.close()


def test_stop_teardown_is_resilient_to_a_failing_instance_cleanup(tmp_path):
    """T4: a raising instance teardown must not strand the stop.

    The run transition has already landed, so the run is stopped regardless.
    The hazard the old ordering carried was that a raising
    ``_stop_owned_instances`` skipped ``run.stopped`` and left assignments live,
    so the orchestrator's recovery loop later resurrected the seat. The teardown
    must now release every seat, still write the terminal event, and record the
    failed step instead of aborting.
    """
    platform = FixturePlatform(
        [PlatformChallenge(
            external_id="teardown-remote", title="Teardown remote", category="web",
            description="teardown fixture", remote=True,
        )],
        flags={"teardown-remote": "fixture:correct"},
        remote_limit=1,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "teardown"
    )
    try:
        run_id = service.start(workflow_id, "teardown-run")["run"]["id"]
        service.tick()
        assert service.store.assignments(run_id, active_only=True)

        def boom(_run):
            raise RuntimeError("docker daemon unreachable")

        service._stop_owned_instances = boom
        revision = service.store.get_run(run_id)["revision"]
        snapshot = service.control(run_id, "stop", revision)

        assert snapshot["run"]["status"] == "stopped"
        assert service.store.assignments(run_id, active_only=True) == []
        kinds = {event["kind"] for event in service.store.run_events(run_id)}
        assert "run.stopped" in kinds
        assert "run.teardown_failed" in kinds
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


def test_resume_restores_original_member_challenge_pairing(tmp_path):
    platform = FixturePlatform(
        [
            PlatformChallenge(
                external_id=f"resume-{index}", title=f"Resume {index}",
                category="misc", description="resume fixture",
            )
            for index in range(4)
        ],
        flags={f"resume-{index}": f"flag{{resume-{index}}}" for index in range(4)},
        remote_limit=0,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "resume"
    )
    try:
        snapshot = service.start(workflow_id, "resume-run")
        run_id = snapshot["run"]["id"]
        service.tick()
        service.tick()
        original = {
            item["challenge_id"]: (item["member"], item["session_id"])
            for item in service.store.assignments(run_id, active_only=True)
        }
        assert len(original) == 4

        paused = service.control(
            run_id, "pause", service.store.get_run(run_id)["revision"]
        )
        assert paused["run"]["status"] == "paused"
        assert service.store.assignments(run_id, active_only=True) == []

        with db.connect() as connection:
            rows = connection.execute(
                "SELECT id,metadata FROM competition_challenges"
            ).fetchall()
            ordered = sorted(
                rows,
                key=lambda row: (str(row["metadata"].get("ready_at") or ""), row["id"]),
            )
            for index, row in enumerate(reversed(ordered)):
                connection.execute(
                    """UPDATE competition_challenges
                       SET metadata = jsonb_set(metadata, '{ready_at}', to_jsonb(%s::text), true)
                       WHERE id=%s""",
                    (f"2026-01-01T00:00:0{index}+00:00", row["id"]),
                )

        resumed = service.control(
            run_id, "resume", service.store.get_run(run_id)["revision"]
        )
        assert resumed["run"]["status"] == "running"
        service.tick()

        restored = {
            item["challenge_id"]: (item["member"], item["session_id"])
            for item in service.store.assignments(run_id, active_only=True)
        }
        assert restored == original
        with db.connect() as connection:
            restored_events = connection.execute(
                """SELECT count(*) AS count FROM competition_run_events
                   WHERE run_id=%s AND kind='assignment.restored'""",
                (run_id,),
            ).fetchone()
        assert restored_events["count"] == 4
    finally:
        service.shutdown()
        db.close()


def test_a_silent_worker_is_reported_as_stalled_instead_of_quietly_expiring(tmp_path):
    """P5b end to end: the coordinator stops propping up a dead worker.

    Previously the tick renewed every seat it owned, so a Member that had
    stopped turning held its challenge until the deadline with nothing in the
    event stream to explain it. Now the stall is both refused a renewal and
    recorded.
    """
    platform = FixturePlatform(
        [
            PlatformChallenge(
                external_id="stall-1", title="Stall 1", category="misc",
                description="stall fixture",
            )
        ],
        flags={"stall-1": "flag{stall-1}"},
        remote_limit=0,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "stall"
    )
    try:
        snapshot = service.start(workflow_id, "stall-run")
        run_id = snapshot["run"]["id"]
        service.tick()
        service.tick()
        active = service.store.assignments(run_id, active_only=True)
        assert active, "fixture produced no assignment to stall"
        seat = active[0]

        with db.connect() as connection:
            assert connection.execute(
                """SELECT 1 FROM agent_session_writers
                   WHERE session_id=%s AND worker_kind='competition-assignment'""",
                (seat["session_id"],),
            ).fetchone(), "assign() did not create a writer row"
            connection.execute(
                """UPDATE agent_session_writers
                   SET lease_expires_at=now()-interval '1 second'
                   WHERE session_id=%s""",
                (seat["session_id"],),
            )
            connection.execute(
                """UPDATE competition_assignments
                   SET lease_expires_at=now()+interval '5 seconds' WHERE id=%s""",
                (seat["id"],),
            )

        service.tick()

        with db.connect() as connection:
            stalled_events = connection.execute(
                """SELECT payload FROM competition_run_events
                   WHERE run_id=%s AND kind='assignment.stalled'""",
                (run_id,),
            ).fetchall()
            extended = connection.execute(
                """SELECT lease_expires_at>now()+interval '30 seconds' AS extended
                   FROM competition_assignments WHERE id=%s""",
                (seat["id"],),
            ).fetchone()
        assert len(stalled_events) == 1
        assert stalled_events[0]["payload"]["assignment_id"] == seat["id"]
        assert extended["extended"] is False

        service.tick()
        with db.connect() as connection:
            again = connection.execute(
                """SELECT count(*) AS count FROM competition_run_events
                   WHERE run_id=%s AND kind='assignment.stalled'""",
                (run_id,),
            ).fetchone()
        assert again["count"] == 1
    finally:
        service.shutdown()
        db.close()


def test_watchdog_hands_a_dead_seat_to_another_member_keeping_the_findings(tmp_path):
    """P5c end to end: a quiet seat escalates to a handoff, not a restart.

    Keeping the session id is the point. A reassignment would throw away every
    finding the stalled Member recorded and start the challenge from nothing.
    """
    platform = FixturePlatform(
        [
            PlatformChallenge(
                external_id="wd-1", title="Watchdog 1", category="misc",
                description="watchdog fixture",
            )
        ],
        flags={"wd-1": "flag{wd-1}"},
        remote_limit=0,
    )
    db, service, _state, _ops, workflow_id = _new_fixture_service(
        tmp_path, platform, "watchdog"
    )
    try:
        snapshot = service.start(workflow_id, "watchdog-run")
        run_id = snapshot["run"]["id"]
        service.tick()
        service.tick()
        active = service.store.assignments(run_id, active_only=True)
        primaries = [item for item in active if item["role"] == "primary"]
        assert primaries, "fixture produced no primary assignment to stall"
        seat = primaries[0]

        service.store.append_event(
            seat["session_id"], "evidence-1", "tool_result",
            {"evidence": "found an admin panel"},
            assignment_id=seat["id"], owner=seat["lease_owner"],
            epoch=seat["epoch"],
        )

        with db.connect() as connection:
            connection.execute(
                """UPDATE agent_session_writers
                   SET lease_expires_at=now()-interval '1 hour',
                       last_renewed_at=now()-interval '1 hour'
                   WHERE session_id=%s""",
                (seat["session_id"],),
            )
            connection.execute(
                """UPDATE agent_sessions SET created_at=now()-interval '1 hour'
                   WHERE id=%s""",
                (seat["session_id"],),
            )
            connection.execute(
                """UPDATE agent_events SET created_at=now()-interval '1 hour'
                   WHERE session_id=%s""",
                (seat["session_id"],),
            )

        service.tick()

        moved = service.store.find_assignment(
            run_id, seat["challenge_id"], seat["member"]
        )
        current = [
            item for item in service.store.assignments(run_id, active_only=True)
            if item["challenge_id"] == seat["challenge_id"]
            and item["position"] == seat["position"]
        ]
        assert len(current) == 1
        taken = current[0]
        assert moved is None, "the stalled Member still holds the seat"
        assert taken["member"] != seat["member"]
        assert taken["session_id"] == seat["session_id"]

        with db.connect() as connection:
            events = connection.execute(
                """SELECT kind, payload FROM competition_run_events
                   WHERE run_id=%s AND kind IN ('session.handoff','session.handed_off')
                   ORDER BY created_at""",
                (run_id,),
            ).fetchall()
            inherited = connection.execute(
                """SELECT count(*) AS count FROM agent_events
                   WHERE session_id=%s AND event_key='evidence-1'""",
                (seat["session_id"],),
            ).fetchone()
        assert any(row["kind"] == "session.handed_off" for row in events)
        assert inherited["count"] == 1, "the handoff lost the prior finding"

        service.store.append_event(
            taken["session_id"], "evidence-2", "tool_result",
            {"evidence": "continued from the panel"},
            assignment_id=taken["id"], owner=taken["lease_owner"],
            epoch=taken["epoch"],
        )
    finally:
        service.shutdown()
        db.close()
