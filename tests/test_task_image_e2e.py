from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.docker


def _image() -> str:
    return os.environ.get("IPC_TEST_TASK_IMAGE", "ipc-task:latest").strip() or "ipc-task:latest"


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{ROOT / 'scripts'}:/acceptance:ro",
            _image(), "bash", f"/acceptance/{script}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def test_task_image_acceptance():
    result = _run("task_image_acceptance.sh")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]


def test_task_image_cli_e2e():
    result = _run("task_image_e2e.sh")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]
