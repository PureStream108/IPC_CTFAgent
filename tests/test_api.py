from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.blackboard import graph_store
from backend.server.app import create_app
from tests.helpers import write_mock_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolated root + config dir so tests don't touch the repo's data/
    cfgdir = write_mock_config(tmp_path / "config")
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


def test_reason_claim_heartbeat_release(client):
    pid = client.post(
        "/projects",
        json={"title": "R", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]

    r = client.post(f"/projects/{pid}/reason/claim", json={"worker": "diamond", "trigger": "initial"})
    assert r.status_code == 200
    assert r.json()["worker"] == "diamond"
    assert r.json()["trigger"] == "initial"

    r = client.post(f"/projects/{pid}/reason/claim", json={"worker": "aventurine", "trigger": "facts:2"})
    assert r.status_code == 409

    r = client.post(f"/projects/{pid}/reason/heartbeat", json={"worker": "diamond"})
    assert r.status_code == 200
    assert r.json()["worker"] == "diamond"

    r = client.post(f"/projects/{pid}/reason/release", json={"worker": "diamond"})
    assert r.status_code == 200
    assert r.json()["released"] is True
    assert client.get(f"/projects/{pid}").json()["project"]["reason"] is None


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
    project_log = client.app.state.ipc.logger.root / "project_logs" / "A.jsonl"
    assert project_dir.exists()
    assert project_log.exists()
    assert json.loads(project_log.read_text(encoding="utf-8").splitlines()[0])["event"] == "project_created"

    r = client.delete(f"/projects/{pid}")
    assert r.status_code == 204
    assert not project_dir.exists()
    assert not project_log.exists()
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


def test_project_log_filenames_use_title_suffixes(client):
    names = []
    for _ in range(3):
        detail = client.post(
            "/projects",
            json={"title": "Demo", "origin": "o", "goal": "g", "category": "misc"},
        ).json()
        names.append(detail["project"]["log_filename"])
    assert names == ["Demo.jsonl", "Demo01.jsonl", "Demo02.jsonl"]


def test_project_logs_list_and_derive(client):
    detail = client.post(
        "/projects",
        json={"title": "Demo", "origin": "o", "goal": "g", "category": "misc"},
    ).json()
    pid = detail["project"]["id"]

    r = client.get("/logs/projects")
    assert r.status_code == 200
    item = r.json()["logs"][0]
    assert item["project_id"] == pid
    assert item["project_log"]["filename"] == "Demo.jsonl"
    assert item["project_log"]["entries"][0]["event"] == "project_created"
    assert item["llm_log"]["entries"] == []
    assert item["tool_log"]["entries"] == []
    assert item["memory_log"]["entries"] == []

    r = client.post("/logs/derive")
    assert r.status_code == 200
    assert r.json()["files"]["project_logs"] == ["Demo.log"]
    export = client.app.state.ipc.log_export_dir / "project_logs" / "Demo.log"
    assert export.exists()
    assert json.loads(export.read_text(encoding="utf-8").splitlines()[0])["project_id"] == pid
    assert (client.app.state.ipc.log_export_dir / "llm_logs" / "Demo.log").exists()
    assert (client.app.state.ipc.log_export_dir / "memory_logs" / "Demo.log").exists()

    # A repeated export is a new snapshot and never overwrites the first.
    r = client.post("/logs/derive")
    assert r.json()["files"]["project_logs"] == ["Demo01.log"]
    assert export.exists()
    assert (client.app.state.ipc.log_export_dir / "project_logs" / "Demo01.log").exists()


def test_delete_project_preserves_derived_logs(client):
    detail = client.post(
        "/projects",
        json={"title": "Archived", "origin": "o", "goal": "g", "category": "misc"},
    ).json()
    pid = detail["project"]["id"]

    r = client.post("/logs/derive")
    assert r.status_code == 200
    export = client.app.state.ipc.log_export_dir / "project_logs" / "Archived.log"
    assert export.exists()
    exported_text = export.read_text(encoding="utf-8")

    r = client.delete(f"/projects/{pid}")
    assert r.status_code == 204
    assert export.exists()

    r = client.post("/logs/derive")
    assert r.status_code == 200
    assert export.exists()
    assert export.read_text(encoding="utf-8") == exported_text


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
    assert any(link["src"] == "aventurine" and link["dst"] == "diamond" for link in detail["agent_links"])


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


def test_completed_wp_list_and_derive(client):
    detail = client.post(
        "/projects",
        json={"title": "Solved", "origin": "o", "goal": "g", "category": "web"},
    ).json()
    pid = detail["project"]["id"]
    wp_path = client.app.state.ipc.wp_dir / "Solved.md"
    wp_path.write_text("# Solved\n", encoding="utf-8")
    with client.app.state.ipc.db.connect() as conn:
        graph_store.set_wp_path(conn, pid, str(wp_path))
        graph_store.set_status(conn, pid, "running")
        graph_store.set_status(conn, pid, "flag_found")
        graph_store.set_status(conn, pid, "wp_writing")
        graph_store.set_status(conn, pid, "memory_writing")
        graph_store.set_status(conn, pid, "completed")

    r = client.get("/wp/completed")
    assert r.status_code == 200
    item = r.json()["writeups"][0]
    assert item["project_id"] == pid
    assert item["filename"] == "Solved.md"
    assert item["content"] == "# Solved\n"

    r = client.post("/wp/derive")
    assert r.status_code == 200
    export = client.app.state.ipc.wp_export_dir / "Solved.md"
    assert export.exists()
    assert export.read_text(encoding="utf-8") == "# Solved\n"

    r = client.post("/wp/derive")
    assert r.json()["files"] == ["Solved01.md"]
    assert export.read_text(encoding="utf-8") == "# Solved\n"
    assert (client.app.state.ipc.wp_export_dir / "Solved01.md").read_text(
        encoding="utf-8"
    ) == "# Solved\n"


def test_exports_survive_a_fresh_app_state_and_share_one_log_suffix(
    tmp_path, monkeypatch
):
    exports = tmp_path / "persistent-exports"
    monkeypatch.setenv("IPC_WP_EXPORT_DIR", str(exports / "wp"))
    monkeypatch.setenv("IPC_LOG_EXPORT_DIR", str(exports / "logs"))

    def make_snapshot(root: Path, content: str) -> tuple[dict, dict]:
        config_dir = write_mock_config(root / "config")
        app = create_app(root=root)
        with TestClient(app) as current:
            current.app.state.ipc.config_dir = config_dir
            current.app.state.ipc.reload_config()
            detail = current.post(
                "/projects",
                json={
                    "title": "Same:Task",
                    "origin": "o",
                    "goal": "g",
                    "category": "web",
                },
            ).json()
            pid = detail["project"]["id"]
            wp_path = current.app.state.ipc.wp_dir / f"{pid}.md"
            wp_path.write_text(content, encoding="utf-8")
            with current.app.state.ipc.db.connect() as conn:
                graph_store.set_wp_path(conn, pid, str(wp_path))
                for status in (
                    "running",
                    "flag_found",
                    "wp_writing",
                    "memory_writing",
                    "completed",
                ):
                    graph_store.set_status(conn, pid, status)
            return current.post("/wp/derive").json(), current.post(
                "/logs/derive"
            ).json()

    first_wp, first_logs = make_snapshot(tmp_path / "run-one", "# first\n")
    first_wp_path = exports / "wp" / "Same_Task.md"
    first_log_paths = [
        exports / "logs" / group / "Same_Task.log"
        for group in ("project_logs", "llm_logs", "tool_logs", "memory_logs")
    ]
    original_wp = first_wp_path.read_bytes()
    original_logs = {path: path.read_bytes() for path in first_log_paths}

    second_wp, second_logs = make_snapshot(tmp_path / "run-two", "# second\n")

    assert first_wp["files"] == ["Same_Task.md"]
    assert second_wp["files"] == ["Same_Task01.md"]
    assert first_wp_path.read_bytes() == original_wp
    assert (exports / "wp" / "Same_Task01.md").read_text(encoding="utf-8") == "# second\n"
    for group in ("project_logs", "llm_logs", "tool_logs", "memory_logs"):
        assert first_logs["files"][group] == ["Same_Task.log"]
        assert second_logs["files"][group] == ["Same_Task01.log"]
        old = exports / "logs" / group / "Same_Task.log"
        new = exports / "logs" / group / "Same_Task01.log"
        assert old.read_bytes() == original_logs[old]
        assert new.exists()
        for line in new.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_export_collision_detection_is_case_insensitive(client):
    detail = client.post(
        "/projects",
        json={"title": "Case", "origin": "o", "goal": "g", "category": "misc"},
    ).json()
    pid = detail["project"]["id"]
    wp_path = client.app.state.ipc.wp_dir / "case-source.md"
    wp_path.write_text("# new\n", encoding="utf-8")
    with client.app.state.ipc.db.connect() as conn:
        graph_store.set_wp_path(conn, pid, str(wp_path))
        for status in (
            "running",
            "flag_found",
            "wp_writing",
            "memory_writing",
            "completed",
        ):
            graph_store.set_status(conn, pid, status)

    target = client.app.state.ipc.wp_export_dir
    target.mkdir(parents=True, exist_ok=True)
    old = target / "CASE.md"
    old.write_bytes(b"old bytes")
    result = client.post("/wp/derive").json()
    assert result["files"] == ["Case01.md"]
    assert old.read_bytes() == b"old bytes"


def test_memory_derive_writes_to_export_dir(client):
    r = client.post(
        "/memory",
        json={"category": "knowledge", "title": "SSTI", "content": "jinja2 payloads", "tags": ["web"]},
    )
    assert r.status_code == 201

    r = client.post("/memory/derive")
    assert r.status_code == 200
    vault = client.app.state.ipc.memory_export_dir / "vault"
    assert Path(r.json()["vault"]) == vault
    assert (vault / "_index.md").exists()
    notes = list((vault / "knowledge").glob("*.md"))
    assert notes and "jinja2 payloads" in notes[0].read_text(encoding="utf-8")


def test_export_and_replay(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "web"}).json()["project"]["id"]
    r = client.get(f"/projects/{pid}/export", params={"format": "yaml"})
    assert r.status_code == 200
    assert "project" in r.text
    r = client.get(f"/projects/{pid}/replay")
    assert r.status_code == 200
    assert any(e["kind"] == "project_created" for e in r.json()["events"])
