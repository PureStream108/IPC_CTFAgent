from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_persists_app_exports_and_claude_native_sessions():
    raw = Path("docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    app = compose["services"]["ipc-app"]
    binds = dict(entry.split(":", 1) for entry in app["volumes"] if ":" in entry)

    # ./data is the app's host bind mount for durable IPC state and exports.
    assert binds.get("./data") == "/app/data"

    # PostgreSQL and Claude native sessions use their own named volumes. Large
    # solver artifacts are shared through the host's ./data bind mount.
    assert set(compose.get("volumes", {})) == {
        "ipc_claude_home",
        "ipc_postgres_data",
    }
    runner_volumes = compose["services"]["ipc-claude-runner"]["volumes"]
    assert "ipc_claude_home:/home/node/.claude" in runner_volumes
    assert {src for src in binds if not src.startswith((".", "/"))} == set()
    for legacy in ("ipc_data", "ipc_memory", "ipc_wp", "ipc_runtime_logs", "ipc_projects"):
        assert legacy not in raw
    for target in ("/app/memory", "/app/wp", "/app/logs", "/app/projects"):
        assert target not in binds.values()

    # Workspaces, attachments, live output and exports all derive from one
    # deployment-shared root below the persistent bind mount.
    env = dict(item.split("=", 1) for item in app["environment"])
    assert env["IPC_ARTIFACT_ROOT"] == "/app/data/artifacts"
    for legacy in (
        "IPC_LOG_EXPORT_DIR",
        "IPC_WP_EXPORT_DIR",
        "IPC_MEMORY_EXPORT_DIR",
    ):
        assert legacy not in env


def test_task_image_contains_ctf_and_container_mcp_runtimes():
    task_dockerfile = Path("docker/member/Dockerfile").read_text(encoding="utf-8")
    app_dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    inventory = Path("backend/tools/member_tools.txt").read_text(encoding="utf-8")

    assert "ripgrep" in task_dockerfile
    assert "pyghidra" in task_dockerfile
    assert "playwright install --with-deps chromium" in task_dockerfile
    assert "GHIDRA_DIRECT_URL=https://github.com/NationalSecurityAgency/ghidra" in task_dockerfile
    assert 'for url in "${GHIDRA_DIRECT_URL}" "${GHIDRA_URL}"' in task_dockerfile
    assert "COPY backend /opt/ipc/backend" in task_dockerfile
    assert "docker.io" in app_dockerfile
    assert "COPY alembic.ini /app/alembic.ini" in app_dockerfile
    assert "alembic upgrade head" in app_dockerfile
    assert "chromium" not in app_dockerfile
    assert "rg/ripgrep" in inventory


def test_compose_builds_task_image_and_app_depends_on_it():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["ipc-task-image"]["image"] == "ipc-task:latest"
    assert "ipc-member-image" not in compose["services"]
    assert "ipc-task-image" in compose["services"]["ipc-app"]["depends_on"]
    assert compose["services"]["ipc-app"]["ports"] == ["8000:8000"]


def test_zap_is_an_opt_in_compose_service():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    zap = compose["services"]["ipc-zap"]
    app = compose["services"]["ipc-app"]

    assert zap["profiles"] == ["zap"]
    assert "ipc-zap" not in app.get("depends_on", [])
    assert any(
        value.startswith("IPC_ZAP_ENABLED=") for value in app["environment"]
    )
