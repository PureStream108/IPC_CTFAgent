"""The detached-job shell text, verified against a real bash.

These scripts only ever run inside the task container, so no unit test would
otherwise execute them -- yet quoting is the main risk: the command is
model-supplied and nested two levels deep (``bash -c 'bash -lc "<command>"'``).
Running the exact strings through a POSIX shell here is what keeps a command
containing quotes, ``$`` or ``;`` from either breaking the wrapper or escaping
it.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from backend.sandbox.task_sandbox import (
    JOBS_DIR,
    job_cancel_script,
    job_dir,
    job_launch_script,
    job_probe_script,
    parse_job_probe,
)

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="requires a POSIX bash")


def posix(path: Path) -> str:
    """Render a local path the way the shell under test sees it.

    In the container every path is already POSIX. On Windows the tests drive
    Git Bash, which needs ``/c/Users/...`` rather than ``C:\\Users\\...``;
    without this the scripts fail on path handling instead of on the quoting
    behaviour they exist to check.
    """
    text = str(path)
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-lc", script], cwd=str(cwd), capture_output=True, text=True,
        timeout=60, check=False,
    )


def launch(directory: Path, command: str, cwd: Path) -> subprocess.CompletedProcess:
    return run(job_launch_script(posix(directory), command, posix(cwd)), cwd)


def probe(directory: Path, cwd: Path) -> dict:
    result = run(job_probe_script(posix(directory)), cwd)
    return parse_job_probe(result.stdout)


def wait_for(predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    return None


def test_job_dir_is_inside_the_workspace_volume_and_sanitized():
    # Inside the volume, because that is what survives a container restart
    # while a docker exec id does not.
    assert job_dir("abc123").startswith(JOBS_DIR)
    # A job id never reaches the shell unsanitized.
    assert "/" not in job_dir("../../etc/passwd").removeprefix(JOBS_DIR + "/")
    assert ";" not in job_dir("a;rm -rf /")


def test_launch_records_pid_and_real_exit_status(tmp_path):
    directory = tmp_path / "job"
    result = launch(directory, "echo one; sleep 1; echo two; exit 5", tmp_path)
    assert result.returncode == 0, result.stderr
    pid = int(result.stdout.strip().splitlines()[-1])
    assert pid > 0

    # While running: alive, with no status recorded yet.
    early = probe(directory, tmp_path)
    assert early["state"] in {"running", "exited"}

    final = wait_for(
        lambda: (lambda v: v if v["state"] == "exited" else None)(
            probe(directory, tmp_path)
        )
    )
    assert final is not None, "job never recorded a status"
    # The real exit code, not a boolean: 5 has to survive the two wrappers.
    assert final["exit_code"] == 5
    assert (directory / "stdout").read_text().split() == ["one", "two"]


def test_launch_survives_a_command_full_of_shell_metacharacters(tmp_path):
    directory = tmp_path / "job"
    # Single quotes, double quotes, $, backticks and ; all at once. If the
    # quoting is wrong this either fails to run or executes the injection.
    command = """printf '%s\\n' "it's \\$HOME; \\`echo hi\\` | done" """
    result = launch(directory, command, tmp_path)
    assert result.returncode == 0, result.stderr

    assert wait_for(
        lambda: probe(directory, tmp_path)["state"] == "exited"
    ), "job never exited"
    written = (directory / "stdout").read_text()
    # $HOME and the backticks were not expanded by the wrapper layers.
    assert "it's $HOME; `echo hi` | done" in written


def test_launch_does_not_let_a_command_escape_into_the_wrapper(tmp_path):
    directory = tmp_path / "job"
    marker = tmp_path / "escaped"
    # A command that tries to close the quoting and run its own shell line.
    command = f"echo safe' ; touch {posix(marker)} ; echo '"
    launch(directory, command, tmp_path)
    wait_for(lambda: probe(directory, tmp_path)["state"] == "exited")
    assert not marker.exists(), "command text escaped the wrapper quoting"


def test_a_vanished_process_without_a_status_file_reads_as_lost(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    # A pid that is certainly not running, and no status file: exactly the
    # shape left behind by a container restart.
    (directory / "pid").write_text("2147483646")
    state = probe(directory, tmp_path)
    assert state["state"] == "lost"
    assert state["exit_code"] is None


def test_cancel_kills_the_whole_process_group(tmp_path):
    directory = tmp_path / "job"
    # The job spawns a child that outlives its parent's own exit. Killing only
    # the leader would leave this grandchild running and holding stdout open.
    child_marker = tmp_path / "child-alive"
    command = (
        f"( while true; do touch {posix(child_marker)}; sleep 1; done ) & "
        "wait"
    )
    launch(directory, command, tmp_path)
    assert wait_for(lambda: child_marker.exists()), "child never started"

    assert run(job_cancel_script(posix(directory)), tmp_path).returncode == 0
    child_marker.unlink()
    time.sleep(3)
    # Nothing recreated the marker, so no descendant survived the cancel.
    assert not child_marker.exists(), "a descendant survived job_cancel"


def test_cancel_reports_failure_when_there_is_no_pid_file(tmp_path):
    directory = tmp_path / "missing"
    directory.mkdir()
    assert run(job_cancel_script(posix(directory)), tmp_path).returncode == 1


def test_probe_parsing_distinguishes_the_three_states():
    assert parse_job_probe("pid=12 status=0 alive=no stdout=5 stderr=0") == {
        "state": "exited", "exit_code": 0, "pid": 12,
        "stdout_bytes": 5, "stderr_bytes": 0,
    }
    assert parse_job_probe("pid=12 status= alive=yes stdout=5 stderr=1")["state"] == "running"
    assert parse_job_probe("pid=12 status= alive=no stdout=0 stderr=0")["state"] == "lost"
    # A signalled process reports a negative status; it is still an exit, and
    # the code must not be dropped to None.
    signalled = parse_job_probe("pid=12 status=-9 alive=no stdout=0 stderr=0")
    assert signalled["state"] == "exited"
    assert signalled["exit_code"] == -9


def test_a_recycled_pid_is_not_mistaken_for_the_original_job(tmp_path):
    """A container restart resets pids, so pid alone is not identity.

    Without the start-time check a stale pid file that happens to match some
    unrelated process makes a long-dead job report as still running forever -
    and the model keeps polling output that will never grow.
    """
    directory = tmp_path / "job"
    launch(directory, "sleep 30", tmp_path)
    live = probe(directory, tmp_path)
    assert live["state"] == "running"
    assert (directory / "starttime").read_text().strip(), "no start time recorded"

    # Same pid, but recorded as having started at a different time: this is
    # what a recycled pid looks like.
    (directory / "starttime").write_text("1\n")
    assert probe(directory, tmp_path)["state"] == "lost"

    run(job_cancel_script(posix(directory)), tmp_path)


def test_a_missing_start_time_does_not_downgrade_a_live_job(tmp_path):
    """Unknown identity must not turn a running job into a lost one.

    The start time comes from /proc, which may be unreadable. Preferring
    "lost" there would abandon work that is genuinely still running.
    """
    directory = tmp_path / "job"
    launch(directory, "sleep 30", tmp_path)
    (directory / "starttime").write_text("")
    assert probe(directory, tmp_path)["state"] == "running"
    run(job_cancel_script(posix(directory)), tmp_path)
