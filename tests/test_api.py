from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.server.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolated root + config dir so tests don't touch the repo's data/
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    # copy the default config files
    import shutil
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("config.yaml", "models.yaml", "limits.yaml"):
        shutil.copy(src / name, cfgdir / name)
    monkeypatch.setenv("IPC_ROOT", str(tmp_path))

    app = create_app(root=tmp_path)
    # point state at the temp config dir
    with TestClient(app) as c:
        c.app.state.ipc.config_dir = cfgdir
        c.app.state.ipc.reload_config()
        yield c


def test_create_and_get_project(client):
    r = client.post("/projects", json={"title": "Web1", "origin": "http://x", "goal": "get flag", "category": "web"})
    assert r.status_code == 201
    detail = r.json()
    pid = detail["project"]["id"]
    assert detail["project"]["category"] == "web"
    assert {f["id"] for f in detail["facts"]} == {"origin", "goal"}
    assert {a["name"] for a in detail["agents"]} == {"ipc", "diamond"}

    r2 = client.get(f"/projects/{pid}")
    assert r2.status_code == 200


def test_list_projects(client):
    client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "pwn"})
    r = client.get("/projects")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["member_count"] == 0


def test_intent_protocol_flow(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "crypto"}).json()["project"]["id"]
    # declare intent
    r = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "factor n", "creator": "diamond"})
    assert r.status_code == 201
    iid = r.json()["id"]
    # claim
    r = client.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "aventurine"})
    assert r.json()["worker"] == "aventurine"
    # double-claim by another worker rejected
    r = client.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "pearl"})
    assert r.status_code == 409
    # conclude
    r = client.post(f"/projects/{pid}/intents/{iid}/conclude", json={"worker": "aventurine", "description": "n = p*q found"})
    assert r.status_code == 200
    assert r.json()["fact"]["description"] == "n = p*q found"


def test_hint_and_attachment(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "misc"}).json()["project"]["id"]
    r = client.post(f"/projects/{pid}/hints", json={"content": "look at exif", "creator": "human"})
    assert r.status_code == 201
    r = client.post(f"/projects/{pid}/attachments", files={"file": ("chal.bin", b"\x00\x01data", "application/octet-stream")})
    assert r.status_code == 200
    assert r.json()["filename"] == "chal.bin"


def test_delete_project_removes_project_files(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "misc"}).json()["project"]["id"]
    r = client.post(f"/projects/{pid}/attachments", files={"file": ("chal.bin", b"data", "application/octet-stream")})
    assert r.status_code == 200
    project_dir = client.app.state.ipc.projects_dir / pid
    assert project_dir.exists()

    r = client.delete(f"/projects/{pid}")
    assert r.status_code == 204
    assert not project_dir.exists()
    assert client.get(f"/projects/{pid}").status_code == 404


def test_delete_last_project_resets_project_counter(client):
    first = client.post(
        "/projects",
        json={"title": "A", "origin": "o", "goal": "g", "category": "misc"},
    ).json()["project"]["id"]
    assert first == "proj_001"
    assert client.delete(f"/projects/{first}").status_code == 204

    second = client.post(
        "/projects",
        json={"title": "B", "origin": "o", "goal": "g", "category": "misc"},
    ).json()["project"]["id"]
    assert second == "proj_001"


def test_report_submission(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "web"}).json()["project"]["id"]
    r = client.post(f"/projects/{pid}/reports", json={
        "member": "aventurine", "progress": "found login", "difficulty": "high",
        "steps": ["recon", "found /admin"], "directions": ["try sqli", "try ssti"],
        "knowledge": ["sqli"],
    })
    assert r.status_code == 201
    assert r.json()["difficulty"] == "high"
    # report drew a Member->Diamond link
    detail = client.get(f"/projects/{pid}").json()
    assert any(l["src"] == "aventurine" and l["dst"] == "diamond" for l in detail["agent_links"])


def test_complete_marks_flag(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "web"}).json()["project"]["id"]
    fid = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "x", "creator": "diamond"}).json()
    # conclude to make a fact
    client.post(f"/projects/{pid}/intents/{fid['id']}/heartbeat", json={"worker": "aventurine"})
    fact = client.post(f"/projects/{pid}/intents/{fid['id']}/conclude", json={"worker": "aventurine", "description": "rce achieved"}).json()["fact"]
    r = client.post(f"/projects/{pid}/complete", json={"from": [fact["id"]], "description": "flag captured", "worker": "aventurine", "flag": "flag{win}"})
    assert r.status_code == 200
    assert r.json()["to"] == "goal"
    detail = client.get(f"/projects/{pid}").json()
    # /complete triggers the orchestrator finalize pipeline -> completed
    assert detail["project"]["status"] in ("flag_found", "completed")
    assert detail["project"]["flag"] == "flag{win}"


def test_memory_api(client):
    r = client.post("/memory", json={"category": "knowledge", "title": "T", "content": "C", "tags": ["web"]})
    assert r.status_code == 201
    assert client.get("/memory").json()
    r = client.get("/memory/search", params={"q": "web"})
    assert r.status_code == 200


def test_config_api_update_and_redaction(client):
    r = client.get("/config")
    assert r.status_code == 200
    assert "diamond" in r.json()
    r = client.put("/config", json={"diamond": {"api_format": "openai", "api_key": "secretkey", "base_url": "http://u"}})
    assert r.status_code == 200
    assert r.json()["diamond"]["api_key_set"] is True
    assert "secretkey" not in str(r.json())  # redacted


def test_config_runtime_api(client):
    r = client.get("/config/runtime")
    assert r.status_code == 200
    body = r.json()
    assert "runtime" in body
    assert "limits" in body
    assert "limiter" in body
    assert "pool" in body
    assert "orchestrator" in body


def test_logs_toggle(client):
    r = client.put("/logs/status", json={"enabled": False})
    assert r.json()["enabled"] is False
    r = client.put("/logs/status", json={"enabled": True})
    assert r.json()["enabled"] is True


def test_export_and_replay(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "web"}).json()["project"]["id"]
    r = client.get(f"/projects/{pid}/export", params={"format": "yaml"})
    assert r.status_code == 200
    assert "project" in r.text
    r = client.get(f"/projects/{pid}/replay")
    assert r.status_code == 200
    assert any(e["kind"] == "project_created" for e in r.json()["events"])
