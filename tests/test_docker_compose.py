from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_persists_only_exports_via_data_bind_mount():
    raw = Path("docker-compose.yml").read_text(encoding="utf-8")
    compose = yaml.safe_load(raw)
    app = compose["services"]["ipc-app"]
    binds = dict(entry.split(":", 1) for entry in app["volumes"] if ":" in entry)

    # ./data is a host bind mount and the only state that survives `down`.
    assert binds.get("./data") == "/app/data"

    # Runtime state is in RAM / ephemeral, not held in named volumes.
    assert "volumes" not in compose
    assert {src for src in binds if not src.startswith((".", "/"))} == set()
    for legacy in ("ipc_data", "ipc_memory", "ipc_wp", "ipc_runtime_logs", "ipc_projects"):
        assert legacy not in raw
    for target in ("/app/memory", "/app/wp", "/app/logs", "/app/projects"):
        assert target not in binds.values()

    # Everything the UI derives is written under the persistent bind mount.
    env = dict(item.split("=", 1) for item in app["environment"])
    assert env["IPC_LOG_EXPORT_DIR"] == "/app/data/logs"
    assert env["IPC_WP_EXPORT_DIR"] == "/app/data/Wp"
    assert env["IPC_MEMORY_EXPORT_DIR"] == "/app/data/memory"


def test_task_image_contains_ctf_and_container_mcp_runtimes():
    task_dockerfile = Path("docker/member/Dockerfile").read_text(encoding="utf-8")
    app_dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    inventory = Path("backend/tools/member_tools.txt").read_text(encoding="utf-8")

    assert "ripgrep" in task_dockerfile
    assert "pyghidra" in task_dockerfile
    assert "playwright install --with-deps chromium" in task_dockerfile
    assert "COPY backend /opt/ipc/backend" in task_dockerfile
    assert "docker.io" in app_dockerfile
    assert "chromium" not in app_dockerfile
    assert "rg/ripgrep" in inventory


def test_compose_builds_task_image_and_app_depends_on_it():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["ipc-task-image"]["image"] == "ipc-task:latest"
    assert "ipc-member-image" not in compose["services"]
    assert "ipc-task-image" in compose["services"]["ipc-app"]["depends_on"]
