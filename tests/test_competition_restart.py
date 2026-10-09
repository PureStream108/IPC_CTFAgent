from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from backend.competition.fixture import FixturePlatform
from backend.competition.service import CompetitionService
from backend.competition.store import CompetitionConflict
from backend.competition.workspace import SharedWorkspace
from backend.ops.models import PlatformWorkflowSpec
from backend.platform.mapping import PlatformChallenge

from tests.test_competition_service_fixture import (
    _FixtureConfig,
    _FixtureLogger,
    _FixtureOps,
    _FixtureOrchestrator,
    _spec,
)

pytestmark = pytest.mark.postgres


def test_coordinator_restart_recovers_session_checkpoint_artifact_and_deadline(tmp_path: Path):
    from backend.persistence.database import Database

    db = Database().configure()
    workflow_id = "restart-workflow"
    spec: PlatformWorkflowSpec = _spec()
    digest = hashlib.sha256(
        json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with db.connect() as connection:
        connection.execute(
            """INSERT INTO workflows
               (id,source,name,spec_json,spec_digest,status,confirmed_digest,created_at,updated_at)
               VALUES (%s,'test',%s,%s,%s,'confirmed',%s,now(),now())""",
            (workflow_id, spec.name, Jsonb(spec.model_dump(mode="json")), digest, digest),
        )

    platform = FixturePlatform(
        [PlatformChallenge(external_id="restart", title="Restart", category="misc", description="fixture")],
        flags={"restart": "flag{restart}"},
    )
    ops = _FixtureOps(db, {
        "id": workflow_id,
        "status": "confirmed",
        "confirmed_digest": digest,
        "spec_digest": digest,
        "spec": spec,
    })

    def make_service(owner: str):
        state = SimpleNamespace(
            db=db,
            instance_id=owner,
            artifact_root=tmp_path / owner / "artifacts",
            wp_dir=tmp_path / owner / "artifacts" / "writeups",
            config=_FixtureConfig(),
            logger=_FixtureLogger(),
            orchestrator=None,
        )
        state.artifact_root.mkdir(parents=True, exist_ok=True)
        state.wp_dir.mkdir(parents=True, exist_ok=True)
        service = CompetitionService(
            state,
            tick_interval=0.01,
            platform_factory=lambda _workflow_id, _spec: platform,
            ops_service=ops,
        )
        state.orchestrator = _FixtureOrchestrator(service)
        return service, state

    first, first_state = make_service("old-coordinator")
    second = second_state = None
    try:
        snapshot = first.start(workflow_id, "restart-run")
        run_id = snapshot["run"]["id"]
        first.tick()
        first.tick()
        original = first.store.assignments(run_id, active_only=True)[0]
        challenge = first.store.challenges(run_id)[0]
        deadline_before = challenge["deadline_at"]
        first.store.append_event(
            original["session_id"], "before-restart", "tool_call", {"command": "id"},
            assignment_id=original["id"], owner=original["lease_owner"], epoch=original["epoch"],
        )
        first.store.save_checkpoint(
            original, {"turn": 4, "messages": [{"role": "assistant", "content": "resume me"}]}
        )
        workspace = SharedWorkspace(db, tmp_path / "shared", min_free_bytes=0)
        workspace.write(
            challenge["id"], "notes/result.txt", b"persisted result",
            session_id=original["session_id"], assignment_id=original["id"],
            lease_owner=original["lease_owner"], lease_epoch=original["epoch"],
            expected_version=None,
        )

        # Simulate both coordinator and worker disappearing.  No application
        # code is allowed to rewrite first_assigned_at/deadline_at on recovery.
        with db.connect() as connection:
            connection.execute(
                "UPDATE competition_runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                (run_id,),
            )
            connection.execute(
                "UPDATE competition_assignments SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                (original["id"],),
            )

        first.shutdown()
        second, second_state = make_service("new-coordinator")
        second.tick()
        recovered = second.store.assignments(run_id, active_only=True)[0]
        challenge_after = second.store.challenges(run_id)[0]
        assert recovered["id"] == original["id"]
        assert recovered["session_id"] == original["session_id"]
        assert recovered["lease_owner"] == "new-coordinator:competition"
        assert recovered["epoch"] == original["epoch"] + 1
        assert challenge_after["deadline_at"] == deadline_before
        assert second.store.session(original["session_id"])["checkpoint"]["turn"] == 4
        metadata, content = workspace.read(challenge["id"], "notes/result.txt")
        assert metadata["version"] == 1
        assert content == b"persisted result"
        with pytest.raises(CompetitionConflict):
            second.store.append_event(
                original["session_id"], "stale-after-restart", "tool_call", {},
                assignment_id=original["id"], owner=original["lease_owner"], epoch=original["epoch"],
            )
    finally:
        if second is not None:
            second.shutdown()
        elif first is not None:
            first.shutdown()
        db.close()
