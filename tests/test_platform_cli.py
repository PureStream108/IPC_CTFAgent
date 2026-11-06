"""Operator CLIs live with the platform they exercise, and stay optional.

Moving these into backend/platform/<name>/ puts them in the wheel, so the only
thing that makes it safe is that their heavy dependency is imported inside
main().  These tests are the fence around that: an import must never pull
playwright or the evaluation SDK, and every entry point must still answer
--help without a network or a browser.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MODULES = [
    "backend.platform.gzctf.check",
    "backend.platform.gzctf.fetch_targets",
    "backend.platform.ret2shell.check",
    "backend.platform.ret2shell.fetch_targets",
    "backend.platform.ret2shell.submit",
    "backend.platform.tsecbench.run",
    "backend.platform.tsecbench.browser_setup",
]

HELP_MODULES = [module for module in MODULES if not module.endswith("browser_setup")]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_modules_import_without_optional_dependencies():
    script = (
        "import importlib, sys\n"
        f"for name in {MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "heavy = sorted({key.split('.')[0] for key in sys.modules}"
        " & {'playwright', 'tsec_benchmark'})\n"
        "print(','.join(heavy))\n"
    )
    result = run("-c", script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


@pytest.mark.parametrize("module", HELP_MODULES)
def test_entry_point_answers_help(module):
    result = run("-m", module, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_the_old_script_paths_are_gone():
    """A stale copy would drift from the module and be run by habit."""

    for name in (
        "tsecbench_run.py",
        "tsecbench_browser_setup.py",
        "gzctf_platform_check.py",
        "fetch_gzctf_targets.py",
        "ret2shell_platform_check.py",
        "fetch_ret2shell_targets.py",
        "ret2shell_submit.py",
    ):
        assert not (ROOT / "scripts" / name).exists(), name
