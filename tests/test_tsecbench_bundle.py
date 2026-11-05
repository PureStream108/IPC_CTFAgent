"""The hosted build context must mirror the checkout, minus secrets.

The hosted image is built from a separate directory (IPC_TsecBench). A stale
copy silently ships an old solver, so the sync tool has to be exact and has to
report drift without writing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "sync_tsecbench_bundle.py"


def run(target: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target), *extra],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def make_source(root: Path) -> Path:
    source = root / "checkout"
    (source / "backend" / "config").mkdir(parents=True)
    (source / "backend" / "__pycache__").mkdir(parents=True)
    (source / "backend" / "server").mkdir(parents=True)
    (source / "frontend").mkdir(parents=True)
    (source / "backend" / "server" / "app.py").write_text("app = 1\n", encoding="utf-8")
    (source / "backend" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"cache")
    (source / "backend" / "config" / "config.example.yml").write_text("log: true\n", encoding="utf-8")
    (source / "backend" / "config" / "config.yaml").write_text("api_key: sk-live\n", encoding="utf-8")
    (source / "frontend" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname = 'ipc'\n", encoding="utf-8")
    (source / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    (source / "LICENSE").write_text("AGPL\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_noise.py").write_text("pass\n", encoding="utf-8")
    return source


def test_sync_copies_only_what_the_image_needs(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "bundle"
    result = run(target, "--source", str(source))
    assert result.returncode == 0, result.stderr
    assert (target / "backend" / "server" / "app.py").is_file()
    assert (target / "frontend" / "index.html").is_file()
    assert (target / "pyproject.toml").is_file()
    assert not (target / "tests").exists()
    assert not (target / "backend" / "__pycache__").exists()
    assert not (target / "backend" / "config" / "config.yaml").exists()


def test_sync_strips_crlf_from_text_files(tmp_path: Path) -> None:
    """A CRLF script breaks the container before the agent starts.

    The shebang becomes "#!/bin/sh\\r" and the kernel cannot resolve the
    interpreter, so the run dies during provisioning with "no such file or
    directory" and never reaches the model.
    """

    source = make_source(tmp_path)
    (source / "backend" / "server" / "app.py").write_bytes(b"app = 1\r\n")
    target = tmp_path / "bundle"

    result = run(target, "--source", str(source))
    assert result.returncode == 0, result.stderr
    assert (target / "backend" / "server" / "app.py").read_bytes() == b"app = 1\n"


def test_second_run_reports_no_changes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "bundle"
    assert run(target, "--source", str(source)).returncode == 0
    again = run(target, "--source", str(source))
    assert again.returncode == 0
    assert "up to date" in again.stdout


def test_check_mode_reports_drift_without_writing(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "bundle"
    assert run(target, "--source", str(source)).returncode == 0
    (source / "backend" / "server" / "app.py").write_text("app = 2\n", encoding="utf-8")
    drift = run(target, "--source", str(source), "--check")
    assert drift.returncode == 1
    assert "update backend" in drift.stdout
    assert (target / "backend" / "server" / "app.py").read_text(encoding="utf-8") == "app = 1\n"


def test_deleted_source_file_is_removed_from_the_bundle(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "bundle"
    assert run(target, "--source", str(source)).returncode == 0
    (source / "frontend" / "index.html").unlink()
    result = run(target, "--source", str(source))
    assert result.returncode == 0
    assert "delete frontend" in result.stdout
    assert not (target / "frontend" / "index.html").exists()


def test_local_files_outside_the_mirrored_trees_survive(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    target = tmp_path / "bundle"
    assert run(target, "--source", str(source)).returncode == 0
    (target / "Dockerfile.hosted").write_text("FROM scratch\n", encoding="utf-8")
    (target / "hosted").mkdir()
    (target / "hosted" / "entrypoint.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert run(target, "--source", str(source)).returncode == 0
    assert (target / "Dockerfile.hosted").is_file()
    assert (target / "hosted" / "entrypoint.sh").is_file()
