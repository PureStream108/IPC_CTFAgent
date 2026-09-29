from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_persists_app_exports_and_postgres_state():
    raw = Path("docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    app = compose["services"]["ipc-app"]
    binds = dict(entry.split(":", 1) for entry in app["volumes"] if ":" in entry)

    # ./data is the app's host bind mount for durable IPC state and exports.
    assert binds.get("./data") == "/app/data"

    # PostgreSQL is the only named volume. Every agent runs in-process through
    # the unified runtime, so there is no sidecar session store to persist.
    assert set(compose.get("volumes", {})) == {"ipc_postgres_data"}
    assert "ipc-claude-runner" not in compose["services"]
    assert "claude" not in raw.lower()
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
    assert "pip3 install --no-cache-dir playwright" in task_dockerfile
    assert "playwright install" not in task_dockerfile
    assert "IPC_CHROME_BIN" not in task_dockerfile
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


def test_zap_is_not_deployed():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "ipc-zap" not in compose["services"]
    assert not any("ZAP" in value for value in compose["services"]["ipc-app"]["environment"])
