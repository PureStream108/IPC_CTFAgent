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


def _two_challenge_platform(monkeypatch):
    """Serve two challenges so a selection can be a strict subset."""

    class Response:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "web-1", "name": "Fixture Web", "category": "web",
                     "description": "controlled", "files": []},
                    {"id": "pwn-2", "name": "Fixture Pwn", "category": "pwn",
                     "description": "controlled", "files": []},
                ]
            }

        def close(self):
            return None

    monkeypatch.setattr(
        "backend.ops.network._pinned_request",
        lambda method, url, **kwargs: Response(),
    )


def _confirmed_workflow(client):
    created = client.post("/api/ops/workflows", json={"workflow": workflow()})
    assert created.status_code == 201
    workflow_id = created.json()["id"]
    confirmed = client.post(
        f"/api/ops/workflows/{workflow_id}/confirm",
        json={"confirmation_phrase": f"CONFIRM WORKFLOW {workflow_id}"},
    )
    assert confirmed.status_code == 200
    return workflow_id


def test_start_passes_the_challenge_selection_through(client, monkeypatch):
    """Starting by challenge needed service support the API never exposed."""
    _two_challenge_platform(monkeypatch)
    workflow_id = _confirmed_workflow(client)

    started = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-start", "select": ["pwn-2", "pwn-2"]},
    )
    assert started.status_code == 201
    snapshot = started.json()
    assert [item["external_id"] for item in snapshot["challenges"]] == ["pwn-2"]
    run = client.app.state.ipc.competition.store.get_run(snapshot["run"]["id"])
    assert run["config_snapshot"]["selected_external_ids"] == ["pwn-2"]


def test_start_rejects_a_selection_that_is_not_available(client, monkeypatch):
    _two_challenge_platform(monkeypatch)
    workflow_id = _confirmed_workflow(client)

    rejected = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-missing", "select": ["does-not-exist"]},
    )
    assert rejected.status_code == 400
    assert "does-not-exist" in rejected.json()["detail"]

    empty = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-empty", "select": []},
    )
    assert empty.status_code == 422


def test_start_without_a_selection_still_takes_every_challenge(client, monkeypatch):
    _two_challenge_platform(monkeypatch)
    workflow_id = _confirmed_workflow(client)

    started = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-absent"},
    )
    assert started.status_code == 201
    snapshot = started.json()
    assert len(snapshot["challenges"]) == 2
    run = client.app.state.ipc.competition.store.get_run(snapshot["run"]["id"])
    assert "selected_external_ids" not in run["config_snapshot"]


def test_reused_idempotency_key_returns_the_first_run_regardless_of_selection(
    client, monkeypatch
):
    """The key identifies the request, so the stored run wins over a new select."""
    _two_challenge_platform(monkeypatch)
    workflow_id = _confirmed_workflow(client)

    first = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-idem", "select": ["web-1"]},
    )
    assert first.status_code == 201
    again = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-select-idem", "select": ["pwn-2"]},
    )
    assert again.status_code == 201
    assert again.json()["run"]["id"] == first.json()["run"]["id"]
    assert [item["external_id"] for item in again.json()["challenges"]] == ["web-1"]


def test_a_workflow_a_run_was_started_from_cannot_be_deleted(client, monkeypatch):
    """Deleting it would break the run's provenance, and the FK refuses it.

    Without this the console's delete button turned a foreign key violation
    into an opaque 500.
    """
    _two_challenge_platform(monkeypatch)
    workflow_id = _confirmed_workflow(client)
    started = client.post(
        f"/api/workflows/{workflow_id}/start",
        json={"idempotency_key": "api-delete-guard"},
    )
    assert started.status_code == 201

    refused = client.delete(f"/api/ops/workflows/{workflow_id}")
    assert refused.status_code == 409
    assert "competition run" in refused.json()["detail"]
    assert client.get(f"/api/ops/workflows/{workflow_id}").status_code == 200


def test_an_unused_workflow_can_be_deleted(client):
    workflow_id = _confirmed_workflow(client)
    assert client.delete(f"/api/ops/workflows/{workflow_id}").status_code == 204
    assert client.get(f"/api/ops/workflows/{workflow_id}").status_code == 404
    assert client.delete(f"/api/ops/workflows/{workflow_id}").status_code == 404


def test_confirmation_preview_reports_missing_secrets(client):
    """The console needs to explain why authorizing would fail, up front."""
    spec = workflow()
    spec["challenges"]["headers"] = [
        {"name": "Authorization", "secret_name": "api_token", "prefix": "Bearer "}
    ]
    created = client.post("/api/ops/workflows", json={"workflow": spec})
    assert created.status_code == 201
    workflow_id = created.json()["id"]

    preview = client.get(f"/api/ops/workflows/{workflow_id}/confirmation").json()
    assert preview["missing_secrets"] == ["api_token"]
    assert preview["ready"] is False
    assert preview["confirmation_phrase"] == f"CONFIRM WORKFLOW {workflow_id}"

    assert client.put(
        f"/api/ops/workflows/{workflow_id}/secrets",
        json={"secrets": {"api_token": "t0ken"}},
    ).status_code == 200
    ready = client.get(f"/api/ops/workflows/{workflow_id}/confirmation").json()
    assert ready["missing_secrets"] == []
    assert ready["ready"] is True
