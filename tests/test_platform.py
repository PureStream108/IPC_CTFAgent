from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from backend.blackboard import graph_store
from backend.server.app import create_app
from tests.helpers import write_mock_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    config_dir = write_mock_config(tmp_path / "config")
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))
    app = create_app(root=tmp_path)
    with TestClient(app) as test_client:
        test_client.app.state.ipc.config_dir = config_dir
        test_client.app.state.ipc.reload_config()
        yield test_client


def test_platform_preview_import_and_flag_query(client, monkeypatch):
    calls = []
    payload = {
        "payload": {
            "challenges": [
                {
                    "challenge_id": 42,
                    "display_name": "Rendered Web",
                    "kind": "Web Exploitation",
                    "body": "Find the flag in the app",
                    "downloads": ["files/challenge.txt"],
                },
                {
                    "challenge_id": 99,
                    "display_name": "Unknown Category",
                    "kind": "Unmapped",
                    "body": "Fallback category",
                    "downloads": [],
                },
            ]
        }
    }

    class FakeResponse:
        def __init__(self, *, json_body=None, content=b""):
            self._json = json_body
            self._content = content

        def raise_for_status(self):
            return None

        def json(self):
            return self._json

        def iter_content(self, chunk_size):
            yield self._content

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url == "https://ctf.test/api/challenges":
            return FakeResponse(json_body=payload)
        if url == "https://ctf.test/assets/files/challenge.txt":
            return FakeResponse(content=b"attachment data")
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("backend.platform.adapter.requests.get", fake_get)
    mapping = {
        "list_url": "https://ctf.test/api/challenges",
        "list_path": "payload.challenges",
        "id_field": "challenge_id",
        "title_field": "display_name",
        "category_field": "kind",
        "description_field": "body",
        "attachments_field": "downloads",
        "category_map": {"Web Exploitation": "web"},
        "headers": {"Authorization": "Token ephemeral-secret"},
        "attachment_base_url": "https://ctf.test/assets/",
    }

    preview = client.post("/api/platform/challenges", json={"mapping": mapping})
    assert preview.status_code == 200
    challenges = preview.json()["challenges"]
    assert challenges[0]["external_id"] == "42"
    assert challenges[0]["category"] == "web"
    assert challenges[1]["category"] == "misc"

    imported = client.post(
        "/api/platform/import",
        json={"mapping": mapping, "select": ["42"]},
    )
    assert imported.status_code == 201
    item = imported.json()["imported"][0]
    project_id = item["project_id"]
    assert item["external_id"] == "42"

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["external_id"] == "42"
    attachment = detail["attachments"][0]
    assert attachment["filename"] == "challenge.txt"
    assert client.app.state.ipc.attachments_dir(project_id).joinpath("challenge.txt").read_bytes() == b"attachment data"

    initial_flag = client.get(f"/api/flags/{project_id}")
    assert initial_flag.status_code == 200
    assert initial_flag.json()["flag"] is None
    assert initial_flag.json()["submitted"] is False

    with client.app.state.ipc.db.connect() as conn:
        graph_store.set_flag(conn, project_id, "flag{platform}")
        graph_store.set_status(conn, project_id, "completed")
        graph_store.add_broadcast(conn, project_id, "Rendered Web", "flag{platform}")

    flag = client.get(f"/api/flags/{project_id}")
    assert flag.status_code == 200
    assert flag.json()["external_id"] == "42"
    assert flag.json()["flag"] == "flag{platform}"
    assert flag.json()["submitted"] is True
    assert client.get("/api/flags").json()[0]["project_id"] == project_id

    assert all(call[1]["headers"]["Authorization"] == "Token ephemeral-secret" for call in calls)
    assert "ephemeral-secret" not in str(client.app.state.ipc.config.model_dump())
    with client.app.state.ipc.db.connect() as conn:
        stored = " ".join(str(value) for row in conn.execute("SELECT * FROM projects") for value in row)
    assert "ephemeral-secret" not in stored


def test_platform_import_rejects_unknown_selection(client, monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "known", "name": "Known"}]}

    monkeypatch.setattr(
        "backend.platform.adapter.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    response = client.post(
        "/api/platform/import",
        json={
            "mapping": {"list_url": "https://ctf.test/challenges"},
            "select": ["missing"],
        },
    )
    assert response.status_code == 400
