from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_persists_runtime_state_in_named_volumes():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    app = compose["services"]["ipc-app"]
    volumes = app["volumes"]
    mounted_targets = {entry.split(":", 1)[1] for entry in volumes if ":" in entry}

    for target in ("/app/data", "/app/memory", "/app/wp", "/app/logs", "/app/projects"):
        assert target in mounted_targets

    named_sources = {
        entry.split(":", 1)[0]
        for entry in volumes
        if ":" in entry and not entry.startswith(".") and not entry.startswith("/")
    }
    assert named_sources <= set(compose["volumes"])


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
