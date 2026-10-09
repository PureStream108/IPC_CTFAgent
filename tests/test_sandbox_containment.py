"""Containment: solver commands must stay inside the task container.

A real run escaped the sandbox in two ways -- it read an exported
``ISCTF2025-WriteUp.pdf`` out of the deployment's artifact tree, and it ran
``/d/Language/Python314/python`` instead of the container's interpreter. Both
shapes are regression-tested here, together with the configuration guard that
stops the host-backed ``local`` backend from being selected by accident.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.core.config import LOCAL_SANDBOX_ENV, load_config
from backend.sandbox.command_policy import host_path_violation


def _config_dir(tmp_path: Path, backend: str) -> Path:
    directory = tmp_path / "config"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.yaml").write_text(
        "diamond:\n  api_format: mock\nruntime:\n"
        f"  sandbox_backend: {backend}\n",
        encoding="utf-8",
    )
    source = Path(__file__).resolve().parent.parent / "backend" / "config"
    for name in ("models.yaml", "limits.yaml"):
        shutil.copy(source / name, directory / name)
    return directory


@pytest.mark.parametrize(
    "command",
    [
        "cd /workspace/shared && ls -la cover.png",
        "cat /flag",
        "python3 -c 'import numpy'",
        "cd /tmp/i031 && mkdir -p pg && python3 solve.py",
        "grep -ra flag /workspace/attachments",
        "pip install pymupdf",
    ],
)
def test_container_commands_are_allowed(command):
    assert host_path_violation(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "p='D:/Desktop/Codex/IPC_CTFAgent/.qa-artifacts/isctf-real-20260927-150115"
        "/artifacts/projects/proj_104/sandbox/shared/work_i001/ISCTF2025-WriteUp.pdf'",
        "cd /workspace/shared && /d/Language/Python314/python - << 'EOF'",
        r"cat C:\Users\Administrator\AppData\Local\Temp\amber\grid.py",
        "chroot /host /bin/bash -lc id",
        "ls /host/etc/passwd",
        "cat data/artifacts/writeups/proj_104.md",
        "ls .qa-artifacts",
    ],
)
def test_host_paths_are_blocked(command):
    violation = host_path_violation(command)
    assert violation is not None
    assert "task container" in violation


def test_empty_command_is_not_a_violation():
    assert host_path_violation("") is None
    assert host_path_violation(None) is None


def test_local_sandbox_backend_is_upgraded_to_docker(tmp_path, monkeypatch):
    monkeypatch.delenv(LOCAL_SANDBOX_ENV, raising=False)
    config = load_config(_config_dir(tmp_path, "local"))
    assert config.runtime.sandbox_backend == "docker"
    assert config.runtime.sandbox_backend_forced is True
    assert any("upgraded to 'docker'" in warning for warning in config.startup_warnings())
    assert not any("sandbox" in error for error in config.startup_errors())


def test_local_sandbox_backend_requires_an_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv(LOCAL_SANDBOX_ENV, "1")
    config = load_config(_config_dir(tmp_path, "local"))
    assert config.runtime.sandbox_backend == "local"
    assert config.runtime.sandbox_backend_forced is False
    assert config.startup_warnings() == []


def test_docker_sandbox_backend_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.delenv(LOCAL_SANDBOX_ENV, raising=False)
    config = load_config(_config_dir(tmp_path, "docker"))
    assert config.runtime.sandbox_backend == "docker"
    assert config.runtime.sandbox_backend_forced is False
    assert config.startup_warnings() == []
