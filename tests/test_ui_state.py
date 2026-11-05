from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.core.postprocess_store import claim_next_job
from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfgdir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as c:
        setup_test_auth(c)
        c.app.state.ipc.config_dir = cfgdir
        c.app.state.ipc.reload_config()
        yield c


def _create_project(client: TestClient, title: str) -> str:
    response = client.post(
        "/projects",
        json={"title": title, "origin": "https://ctf.example", "goal": "get flag", "category": "web"},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]["id"]


def test_wp_status_is_a_persisted_hot_switch(client, monkeypatch):
    state = client.app.state.ipc
    monkeypatch.delenv("IPC_WP_JOBS", raising=False)

    assert client.get("/wp/status").json() == {"enabled": True}
    assert state.competition.writeups_active() is True

    response = client.put("/wp/status", json={"enabled": False})
    assert response.status_code == 200
    assert response.json() == {"enabled": False}
    # The switch is hot: the running CompetitionService reads config, not a
    # value copied during startup.
    assert state.config.wp_enabled is False
    assert state.competition.writeups_active() is False
    assert client.get("/wp/status").json() == {"enabled": False}
    saved = (Path(state.config_dir) / "config.yaml").read_text(encoding="utf-8")
    assert "wp_enabled: false" in saved

    assert client.put("/wp/status", json={"enabled": True}).json() == {"enabled": True}
    assert state.competition.writeups_active() is True


def test_wp_off_keeps_postprocess_writeup_jobs_pending(client):
    state = client.app.state.ipc
    project_id = _create_project(client, "WP gate")
    # Insert and claim inside one transaction: the uncommitted job is visible
    # only to this connection, so the running orchestrator cannot race the
    # assertion by claiming it first.
    with state.db.connect() as conn:
        conn.execute(
            "INSERT INTO postprocess_jobs (project_id, kind, idempotency_key) "
            "VALUES (%s, 'writeup', %s)",
            (project_id, f"{project_id}:writeup"),
        )
        assert claim_next_job(conn, "test-worker", writeups_enabled=False) is None
        job = claim_next_job(conn, "test-worker", writeups_enabled=True)
    assert job is not None
    assert job.kind == "writeup"
    assert job.project_id == project_id


def test_projects_competitions_group_runs_and_imports(client):
    run_project = _create_project(client, "Run challenge")
    imported_project = _create_project(client, "Imported challenge")
    _create_project(client, "Manual challenge")
    state = client.app.state.ipc
    spec = '{"name": "GZCTF Demo", "challenges": {"platform": "gzctf"}}'
    snapshot = (
        '{"workflow_spec": {"name": "GZCTF Demo", '
        '"challenges": {"platform": "gzctf"}}}'
    )
    with state.db.connect() as conn:
        conn.execute(
            "INSERT INTO workflows (id, source, name, spec_json, spec_digest, status, created_at, updated_at) "
            "VALUES ('wf-demo', 'test', 'GZCTF Demo', %s::jsonb, 'digest-demo', 'confirmed', now(), now())",
            (spec,),
        )
        conn.execute(
            "INSERT INTO competition_runs (id, workflow_id, identity_key, idempotency_key, config_snapshot) "
            "VALUES ('run-demo', 'wf-demo', 'https://ctf.example/game/12/team/7', 'idem-demo', %s::jsonb)",
            (snapshot,),
        )
        conn.execute(
            "INSERT INTO competition_challenges "
            "(id, identity_key, external_id, run_id, project_id, state, metadata) "
            "VALUES ('chal-demo', 'https://ctf.example/game/12/team/7', 'ext-run', "
            "'run-demo', %s, 'discovered', '{}'::jsonb)",
            (run_project,),
        )
        conn.execute(
            "INSERT INTO workflow_challenges (workflow_id, external_id, project_id) "
            "VALUES ('wf-demo', 'ext-import', %s)",
            (imported_project,),
        )

    data = client.get("/projects/competitions").json()
    assert len(data["competitions"]) == 1, data
    group = data["competitions"][0]
    assert group["id"] == "workflow:wf-demo"
    assert group["workflow_id"] == "wf-demo"
    assert group["label"] == "GZCTF Demo"
    assert group["platform"] == "gzctf"
    assert group["count"] == 2
    assert set(group["project_ids"]) == {run_project, imported_project}


def test_projects_competitions_label_falls_back_to_identity(client):
    project_id = _create_project(client, "Identified challenge")
    state = client.app.state.ipc
    with state.db.connect() as conn:
        conn.execute(
            "INSERT INTO workflows (id, source, name, spec_json, spec_digest, status, created_at, updated_at) "
            "VALUES ('wf-ident', 'test', '', '{}'::jsonb, 'digest-ident', 'confirmed', now(), now())"
        )
        conn.execute(
            "INSERT INTO competition_runs (id, workflow_id, identity_key, idempotency_key, config_snapshot) "
            "VALUES ('run-ident', 'wf-ident', 'https://ctf.example/game/12/team/7', 'idem-ident', '{}'::jsonb)"
        )
        conn.execute(
            "INSERT INTO competition_challenges "
            "(id, identity_key, external_id, run_id, project_id, state, metadata) "
            "VALUES ('chal-ident', 'https://ctf.example/game/12/team/7', 'ext-ident', "
            "'run-ident', %s, 'discovered', '{}'::jsonb)",
            (project_id,),
        )

    data = client.get("/projects/competitions").json()
    group = next(item for item in data["competitions"] if item["id"] == "workflow:wf-ident")
    assert group["label"] == "ctf.example/game/12"


def test_project_detail_route_still_wins_after_competitions(client):
    project_id = _create_project(client, "Route check")
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["project"]["id"] == project_id
