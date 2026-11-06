from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.blackboard import edge_store, graph_store
from backend.core.config import MEMBER_NAMES
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


def test_get_project_retries_once_on_a_deadlock(client, monkeypatch):
    """T12: a single DeadlockDetected on a read is retried transparently."""
    import psycopg.errors

    from backend.api import project as project_api

    pid = client.post(
        "/projects", json={"title": "D", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]

    real_expire = project_api.graph_store.expire_reason_leases
    calls = {"n": 0}

    def flaky(conn, timeout, project_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return real_expire(conn, timeout, project_id)

    monkeypatch.setattr(project_api.graph_store, "expire_reason_leases", flaky)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert calls["n"] == 2


def test_get_project_degrades_to_last_snapshot_instead_of_500(client, monkeypatch):
    """T12: a persistent deadlock degrades to the last good snapshot, not 500."""
    import psycopg.errors

    from backend.api import project as project_api

    pid = client.post(
        "/projects", json={"title": "E", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    assert client.get(f"/projects/{pid}").status_code == 200

    def always_deadlocks(conn, timeout, project_id=None):
        raise psycopg.errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(project_api.graph_store, "expire_reason_leases", always_deadlocks)
    r = client.get(f"/projects/{pid}")
    assert r.status_code == 200
    assert r.json()["project"]["id"] == pid


def test_start_returns_before_slow_sandbox_bootstrap(client, monkeypatch):
    pid = client.post(
        "/projects",
        json={"title": "Async", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    release = threading.Event()

    def slow_start(_project_id):
        release.wait(5)

    orchestrator = client.app.state.ipc.orchestrator
    monkeypatch.setattr(orchestrator.projects, "start_challenge_env", slow_start)
    started_at = time.monotonic()
    response = client.post(f"/projects/{pid}/start")
    elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    assert elapsed < 1
    body = response.json()
    assert body["phase"] == "queued"
    assert body["run_id"]
    assert body["status"] in {"preflight", "importing", "running"}

    # The one-challenge local run now owns the project, so the legacy
    # per-project stop is refused with an actionable run control instead of
    # silently ending a competition.
    detail = client.get(f"/projects/{pid}").json()
    assert detail["ownership"]["ownership"] == "competition"
    assert detail["ownership"]["run_id"] == body["run_id"]

    stopped = client.post(f"/projects/{pid}/stop")
    release.set()
    assert stopped.status_code == 409
    assert stopped.json()["detail"]["run_id"] == body["run_id"]
    assert stopped.json()["detail"]["control"].endswith("/stop")


def test_intent_protocol_flow(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "crypto"}).json()["project"]["id"]
    r = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "factor n", "creator": "diamond"})
    assert r.status_code == 201
    iid = r.json()["id"]
    r = client.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "aventurine"})
    assert r.json()["worker"] == "aventurine"
    r = client.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "pearl"})
    assert r.status_code == 409
    r = client.post(f"/projects/{pid}/intents/{iid}/conclude", json={"worker": "aventurine", "description": "n = p*q found"})
    assert r.status_code == 200
    assert r.json()["fact"]["description"] == "n = p*q found"


def test_unclaimed_intent_cannot_be_concluded_without_a_lease(client):
    pid = client.post(
        "/projects",
        json={"title": "Fence", "origin": "o", "goal": "g", "category": "crypto"},
    ).json()["project"]["id"]
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "factor n", "creator": "diamond"},
    ).json()

    response = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={"worker": "aventurine", "description": "unfenced result"},
    )

    assert response.status_code == 409
    with client.app.state.ipc.db.connect() as connection:
        row = edge_store.get_intent(connection, pid, intent["id"])
        facts = connection.execute(
            "SELECT description FROM facts WHERE project_id = %s ORDER BY id", (pid,)
        ).fetchall()
    assert row["to_fact_id"] is None
    assert all(fact["description"] != "unfenced result" for fact in facts)


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
    detail = client.get(f"/projects/{pid}").json()
    assert any(link["src"] == "aventurine" and link["dst"] == "diamond" for link in detail["agent_links"])


def test_complete_marks_flag(client):
    pid = client.post("/projects", json={"title": "A", "origin": "o", "goal": "g", "category": "web"}).json()["project"]["id"]
    fid = client.post(f"/projects/{pid}/intents", json={"from": ["origin"], "description": "x", "creator": "diamond"}).json()
    client.post(f"/projects/{pid}/intents/{fid['id']}/heartbeat", json={"worker": "aventurine"})
    fact = client.post(f"/projects/{pid}/intents/{fid['id']}/conclude", json={"worker": "aventurine", "description": "rce achieved"}).json()["fact"]
    r = client.post(f"/projects/{pid}/complete", json={"from": [fact["id"]], "description": "flag captured", "worker": "aventurine", "flag": "flag{win}"})
    assert r.status_code == 200
    assert r.json()["to"] == "goal"
    detail = client.get(f"/projects/{pid}").json()
    assert detail["project"]["status"] in ("flag_found", "solved")
    assert detail["project"]["flag"].startswith("sha256:")


def test_complete_same_flag_is_idempotent(client):
    pid = client.post(
        "/projects",
        json={"title": "Idempotent", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "find flag", "creator": "diamond"},
    ).json()
    client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "aventurine"},
    )
    fact = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={"worker": "aventurine", "description": "flag evidence"},
    ).json()["fact"]
    payload = {
        "from": [fact["id"]],
        "description": "verified",
        "worker": "aventurine",
        "flag": "flag{idempotent}",
    }

    first = client.post(f"/projects/{pid}/complete", json=payload)
    second = client.post(f"/projects/{pid}/complete", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    with client.app.state.ipc.db.connect() as connection:
        rows = connection.execute(
            "SELECT normalized_flag, status FROM flag_submissions WHERE project_id = %s",
            (pid,),
        ).fetchall()
        goal_edges = connection.execute(
            "SELECT id, lease_owner, lease_token, lease_expires_at FROM intents "
            "WHERE project_id = %s AND to_fact_id = 'goal'",
            (pid,),
        ).fetchall()
    assert rows == [{"normalized_flag": "flag{idempotent}", "status": "verified"}]
    assert len(goal_edges) == 1
    assert goal_edges[0]["lease_owner"] is None
    assert goal_edges[0]["lease_token"] is None
    assert goal_edges[0]["lease_expires_at"] is None


def test_complete_different_flag_is_conflict(client):
    pid = client.post(
        "/projects",
        json={"title": "Conflict", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "find flag", "creator": "diamond"},
    ).json()
    client.post(
        f"/projects/{pid}/intents/{intent['id']}/heartbeat",
        json={"worker": "aventurine"},
    )
    fact = client.post(
        f"/projects/{pid}/intents/{intent['id']}/conclude",
        json={"worker": "aventurine", "description": "flag evidence"},
    ).json()["fact"]
    base = {
        "from": [fact["id"]],
        "description": "verified",
        "worker": "aventurine",
        "flag": "flag{first}",
    }
    assert client.post(f"/projects/{pid}/complete", json=base).status_code == 200

    conflict = client.post(
        f"/projects/{pid}/complete",
        json={**base, "flag": "flag{different}"},
    )

    assert conflict.status_code == 409
    assert "different" in conflict.json()["detail"]
    with client.app.state.ipc.db.connect() as connection:
        project = connection.execute(
            "SELECT flag, status FROM projects WHERE id = %s", (pid,)
        ).fetchone()
        submissions = connection.execute(
            "SELECT normalized_flag FROM flag_submissions WHERE project_id = %s ORDER BY id",
            (pid,),
        ).fetchall()
    assert project == {"flag": "flag{first}", "status": "solved"}
    assert submissions == [{"normalized_flag": "flag{first}"}]


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
    assert "secretkey" not in str(r.json())


def test_config_api_has_no_browser_runtime_fields(client):
    body = client.get("/config").json()
    assert body["runtime"] == {}
    assert not any(key.startswith("browser_") for key in body["runtime"])

    response = client.put("/config", json={"runtime": {"browser_event_limit": 300}})
    assert response.status_code in (400, 422)


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
        graph_store.set_status(conn, pid, "solved")

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
    monkeypatch.setenv("IPC_ARTIFACT_ROOT", str(exports))

    def make_snapshot(root: Path, content: str) -> tuple[dict, dict]:
        config_dir = write_mock_config(root / "config")
        app = create_app(root=root)
        with TestClient(app) as current:
            setup_test_auth(current)
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
                graph_store.set_status(conn, pid, "solved")
            wp_snapshot = current.post("/wp/derive").json()
            log_snapshot = current.post("/logs/derive").json()
            with current.app.state.ipc.db.connect() as conn:
                conn.execute("DELETE FROM projects WHERE id = %s", (pid,))
            return wp_snapshot, log_snapshot

    first_wp, first_logs = make_snapshot(tmp_path / "run-one", "# first\n")
    first_wp_path = exports / "exports" / "writeups" / "Same_Task.md"
    first_log_paths = [
        exports / "exports" / "logs" / group / "Same_Task.log"
        for group in ("project_logs", "llm_logs", "tool_logs", "memory_logs")
    ]
    original_wp = first_wp_path.read_bytes()
    original_logs = {path: path.read_bytes() for path in first_log_paths}

    second_wp, second_logs = make_snapshot(tmp_path / "run-two", "# second\n")

    assert first_wp["files"] == ["Same_Task.md"]
    assert second_wp["files"] == ["Same_Task01.md"]
    assert first_wp_path.read_bytes() == original_wp
    assert (exports / "exports" / "writeups" / "Same_Task01.md").read_text(encoding="utf-8") == "# second\n"
    for group in ("project_logs", "llm_logs", "tool_logs", "memory_logs"):
        assert first_logs["files"][group] == ["Same_Task.log"]
        assert second_logs["files"][group] == ["Same_Task01.log"]
        old = exports / "exports" / "logs" / group / "Same_Task.log"
        new = exports / "exports" / "logs" / group / "Same_Task01.log"
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
        graph_store.set_status(conn, pid, "solved")

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

def test_config_view_exposes_the_member_backup_slot_and_key_slots(client):
    body = client.get("/config").json()
    assert body["member_backup"] is None
    assert set(body["member_key_slots"]) == set(MEMBER_NAMES)
    assert set(body["member_key_slots"].values()) == {"primary"}


def test_config_update_sets_and_clears_the_member_backup(client):
    response = client.put(
        "/config",
        json={
            "member_backup": {
                "api_format": "openai",
                "api_key": "backupsecret",
                "base_url": "https://backup.test/v1",
                "model": "deepseek-v4.1-flash",
                "api_surface": "auto",
                "reasoning_effort": "max",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["member_backup"]["api_key_set"] is True
    assert body["member_backup"]["configured"] is True
    assert body["member_backup"]["api_key_preview"] == "bac***"
    assert "backupsecret" not in str(body)
    slots = body["member_key_slots"]
    assert slots["amber"] == "primary"
    assert slots["agate"] == "backup"
    assert sorted(slots.values()) == ["backup"] * 5 + ["primary"] * 5
    assert client.app.state.ipc.config.members[1].api_key == "backupsecret"

    cleared = client.put("/config", json={"clear_member_backup": True})
    assert cleared.status_code == 200
    assert cleared.json()["member_backup"] is None
    assert set(cleared.json()["member_key_slots"].values()) == {"primary"}
    assert all(m.api_key != "backupsecret" for m in client.app.state.ipc.config.members)


def test_config_update_allows_a_partial_backup_that_fails_startup_validation(client):
    body = client.put(
        "/config",
        json={"member_backup": {"base_url": "https://backup.test/v1"}},
    ).json()
    assert body["member_backup"]["configured"] is False
    assert any("Member backup requires" in error for error in body["startup_errors"])


def test_config_update_rejects_clearing_and_setting_the_backup_together(client):
    response = client.put(
        "/config",
        json={"member_backup": {"api_key": "k"}, "clear_member_backup": True},
    )
    assert response.status_code == 400


def test_config_update_validates_member_backup_fields(client):
    response = client.put("/config", json={"member_backup": {"api_format": "claudecode"}})
    assert response.status_code == 422


def test_config_health_reports_the_member_backup_slot(client):
    result = client.post("/config/health").json()["results"]
    assert result["member_backup"]["skipped"] is True


def _write_log_archive(state, archive_id: str, title: str, per_kind: int = 3) -> dict:
    manifest_dir = state.log_export_dir / ".ipc-archives"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    state.wp_export_dir.mkdir(parents=True, exist_ok=True)
    wp_filename = f"{title}.md"
    (state.wp_export_dir / wp_filename).write_text("# Writeup\n", encoding="utf-8")
    log_filename = f"{title}.log"
    log_counts = {}
    for kind, folder_name in state.logger.KINDS.items():
        folder = state.log_export_dir / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / log_filename
        target.write_text(
            "".join(
                json.dumps({"ts": "2024-01-01T00:00:00Z", "event": "archived", "index": index}) + "\n"
                for index in range(per_kind)
            ),
            encoding="utf-8",
        )
        log_counts[kind] = per_kind
    manifest = {
        "version": 1,
        "archive_id": archive_id,
        "project_id": "proj_gone",
        "title": title,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "archived_at": "2024-01-01T00:00:00Z",
        "wp_filename": wp_filename,
        "log_filename": log_filename,
        "log_counts": log_counts,
    }
    (manifest_dir / f"{archive_id}.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_log_catalog_summary_mode_omits_entries_and_empty_projects(client):
    state = client.app.state.ipc
    client.put("/logs/status", json={"enabled": False})
    quiet = client.post(
        "/projects",
        json={"title": "Quiet", "origin": "o", "goal": "g", "category": "misc"},
    ).json()["project"]["id"]
    client.put("/logs/status", json={"enabled": True})
    busy = client.post(
        "/projects",
        json={"title": "Busy", "origin": "o", "goal": "g", "category": "misc"},
    ).json()["project"]["id"]

    calls = []
    original = state.logger.read_log

    def tracked(kind, project_id, limit=500):
        calls.append((kind, project_id))
        return original(kind, project_id, limit)

    state.logger.read_log = tracked
    try:
        response = client.get("/logs/projects", params={"include_entries": "false"})
    finally:
        state.logger.read_log = original
    assert response.status_code == 200
    logs = response.json()["logs"]
    project_ids = [item["project_id"] for item in logs]
    assert busy in project_ids
    assert quiet not in project_ids
    assert calls == []

    item = next(value for value in logs if value["project_id"] == busy)
    assert item["entry_count"] >= 1
    assert item["project_log"]["count"] >= 1
    assert item["llm_log"]["count"] == 0
    assert "entries" not in item["project_log"]
    assert item["archived"] is False

    legacy = client.get("/logs/projects").json()["logs"]
    legacy_item = next(value for value in legacy if value["project_id"] == busy)
    assert legacy_item["project_log"]["entries"][0]["event"] == "project_created"
    assert "count" not in legacy_item["project_log"]


def test_log_catalog_competition_filter(client):
    state = client.app.state.ipc
    run_project = client.post(
        "/projects",
        json={"title": "Run challenge", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    manual_project = client.post(
        "/projects",
        json={"title": "Manual challenge", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    with state.db.connect() as conn:
        conn.execute(
            "INSERT INTO workflows (id, source, name, spec_json, spec_digest, status, created_at, updated_at) "
            "VALUES ('wf-logs', 'test', 'Log Competition', '{}'::jsonb, 'digest-logs', 'confirmed', now(), now())"
        )
        conn.execute(
            "INSERT INTO competition_runs (id, workflow_id, identity_key, idempotency_key, config_snapshot) "
            "VALUES ('run-logs', 'wf-logs', 'https://ctf.example/game/9/team/1', 'idem-logs', '{}'::jsonb)"
        )
        conn.execute(
            "INSERT INTO competition_challenges "
            "(id, identity_key, external_id, run_id, project_id, state, metadata) "
            "VALUES ('chal-logs', 'https://ctf.example/game/9/team/1', 'ext-logs', "
            "'run-logs', %s, 'discovered', '{}'::jsonb)",
            (run_project,),
        )

    params = {"include_entries": "false", "competition_id": "workflow:wf-logs"}
    filtered = client.get("/logs/projects", params=params).json()["logs"]
    assert [item["project_id"] for item in filtered] == [run_project]

    everything = client.get("/logs/projects", params={"include_entries": "false"}).json()["logs"]
    all_ids = {item["project_id"] for item in everything}
    assert {run_project, manual_project} <= all_ids

    assert client.get(
        "/logs/projects", params={"competition_id": "workflow:missing"}
    ).status_code == 404
    assert client.get(
        "/logs/projects", params={"competition_id": "not-a-competition"}
    ).status_code == 404


def test_log_catalog_archive_summary_and_detail(client):
    state = client.app.state.ipc
    _write_log_archive(state, "arch-demo", "Archived Demo", per_kind=3)

    summary = client.get("/logs/projects", params={"include_entries": "false"}).json()["logs"]
    item = next(value for value in summary if value["project_id"] == "archive:arch-demo")
    assert item["archived"] is True
    assert item["source_project_id"] == "proj_gone"
    assert item["entry_count"] == 12
    assert item["project_log"] == {"filename": "Archived Demo.log", "count": 3}

    page = client.get(
        "/logs/archive:arch-demo", params={"limit": 500}
    )
    assert page.status_code == 200
    assert [entry["index"] for entry in page.json()["entries"]] == [0, 1, 2]

    incremental = client.get(
        "/logs/archive:arch-demo", params={"after": 0, "limit": 2}
    ).json()
    assert [entry["index"] for entry in incremental["entries"]] == [0, 1]
    assert incremental["file_id"]
    assert incremental["has_more"] is True

    assert client.get("/logs/archive:missing").status_code == 404
    assert client.get("/logs/archive:..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_log_archive_manifest_rejects_unsafe_filenames(client):
    state = client.app.state.ipc
    manifest = _write_log_archive(state, "arch-unsafe", "Unsafe", per_kind=1)
    manifest["log_filename"] = "../escape.log"
    (state.log_export_dir / ".ipc-archives" / "arch-unsafe.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    summary = client.get("/logs/projects", params={"include_entries": "false"}).json()["logs"]
    assert all(item["project_id"] != "archive:arch-unsafe" for item in summary)
    assert client.get("/logs/archive:arch-unsafe").status_code == 404


def test_run_owned_flag_submission_is_queued_for_the_run(client):
    from backend.core.ipc import submit_flag_candidate

    state = client.app.state.ipc
    pid = client.post(
        "/projects",
        json={"title": "Run owned", "origin": "o", "goal": "g", "category": "web"},
    ).json()["project"]["id"]
    started = client.post(f"/projects/{pid}/start").json()
    assert started["run_id"]

    with state.db.connect() as conn:
        result = submit_flag_candidate(conn, pid, "flag{local}", source="test")
        rows = conn.execute(
            "SELECT status FROM flag_submissions WHERE project_id = %s", (pid,)
        ).fetchall()
    assert result["mode"] != "verified"
    assert [row["status"] for row in rows] == ["pending"]

    with state.db.connect() as conn:
        conn.execute("DELETE FROM flag_submissions WHERE project_id = %s", (pid,))


def test_log_catalog_summary_stays_small_with_many_projects(client):
    """100 projects x 600 entries must not turn the catalog into a body dump."""

    state = client.app.state.ipc
    created = []
    for index in range(100):
        detail = client.post(
            "/projects",
            json={
                "title": f"Scale {index:03d}",
                "origin": "o",
                "goal": "g",
                "category": "misc",
            },
        ).json()
        created.append(detail["project"])

    payload = "".join(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "event": "tick", "index": line}) + "\n"
        for line in range(600)
    )
    for project in created:
        for folder in state.logger.KINDS.values():
            path = state.logger.root / folder / project["log_filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")

    calls = []
    original = state.logger.read_log

    def tracked(kind, project_id, limit=500):
        calls.append((kind, project_id))
        return original(kind, project_id, limit)

    state.logger.read_log = tracked
    try:
        response = client.get("/logs/projects", params={"include_entries": "false"})
    finally:
        state.logger.read_log = original

    assert response.status_code == 200
    logs = response.json()["logs"]
    assert len(logs) >= 100
    assert calls == []
    item = next(entry for entry in logs if entry["project_id"] == created[0]["id"])
    assert item["entry_count"] == 2400
    assert all("entries" not in item[key] for key in ("project_log", "llm_log", "tool_log", "memory_log"))
    assert len(response.content) < 200_000
