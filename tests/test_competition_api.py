from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app
from tests.helpers import setup_test_auth, write_mock_config


pytestmark = pytest.mark.postgres


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_dir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as test_client:
        setup_test_auth(test_client)
        state = test_client.app.state.ipc
        state.config_dir = config_dir
        state.reload_config()
        # Keep this API contract test deterministic; coordinator behavior is
        # exercised independently by store/runtime tests.
        state.competition.shutdown()
        yield test_client


def workflow():
    return {
        "name": "Fixture CTF",
        "competition_id": "game-1",
        "team_id": "team-1",
        "remote_instance_limit": 0,
        "challenges": {
            "list_url": "http://127.0.0.1:9000/challenges",
            "list_path": "data",
            "id_field": "id",
            "title_field": "name",
            "category_field": "category",
            "description_field": "description",
            "attachments_field": "files",
        },
        "submit": {
            "url": "http://127.0.0.1:9000/challenges/{{external_id}}/submit",
            "json_template": {"flag": "{{flag}}"},
            "success_statuses": [200],
            "success_path": "correct",
            "success_values": [True],
            "wrong_values": [False],
        },
        "allow_private_networks": True,
    }


def test_competition_lifecycle_is_revisioned_and_idempotent(client, monkeypatch):
    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [{
                    "id": "web-1", "name": "Fixture Web", "category": "web",
                    "description": "controlled", "files": [],
                }]
            }

        def close(self):
            return None

    monkeypatch.setattr(
        "backend.ops.network._pinned_request",
        lambda method, url, **kwargs: Response(),
    )
    created = client.post("/api/ops/workflows", json={"workflow": workflow()})
    assert created.status_code == 201
    workflow_id = created.json()["id"]
    confirmed = client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    )
    assert confirmed.status_code == 200

    preflight = client.post(f"/api/workflows/{workflow_id}/preflight")
    assert preflight.status_code == 200
    assert preflight.json()["challenge_count"] == 1

    started = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-lifecycle-start"},
    )
    assert started.status_code == 201
    snapshot = started.json()
    run_id = snapshot["run"]["id"]
    assert snapshot["run"]["status"] == "running"
    assert snapshot["capacity"]["total"] == 10
    assert len(client.get(f"/api/runs/{run_id}/members").json()["members"]) == 10
    observations = client.get(f"/api/runs/{run_id}/observations")
    assert observations.status_code == 200
    assert "observations" in observations.json()

    repeated = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-lifecycle-start"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["run"]["id"] == run_id

    revision = snapshot["run"]["revision"]
    assert client.post(f"/api/runs/{run_id}/pause", json={"revision": revision - 1}).status_code == 409
    paused = client.post(f"/api/runs/{run_id}/pause", json={"revision": revision})
    assert paused.status_code == 200
    resumed = client.post(
        f"/api/runs/{run_id}/resume",
        json={"revision": paused.json()["run"]["revision"]},
    )
    assert resumed.status_code == 200
    stopped = client.post(
        f"/api/runs/{run_id}/stop",
        json={"revision": resumed.json()["run"]["revision"]},
    )
    assert stopped.status_code == 200
    assert stopped.json()["run"]["status"] == "stopped"

    events = client.app.state.ipc.competition.store.run_events(run_id)
    assert {event["kind"] for event in events} >= {
        "run.created", "run.started", "run.paused", "run.running", "run.stopped"
    }
