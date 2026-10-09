from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.sandbox.webui_proxy import webui_proxy_manager


def _git_bash_executable() -> Path | None:
    """Return a Git Bash executable for Windows local sandboxes, if present.

    Members are prompted with POSIX shell commands (``find``, ``head``, pipes,
    etc.).  Running those commands through ``cmd.exe`` makes the local backend
    behave differently from the Docker backend and causes avoidable tool
    failures.  Git for Windows provides a small, self-contained Bash that is
    suitable for development-mode sandboxes.
    """
    if os.name != "nt":
        return None

    candidates: list[Path] = []
    configured = os.environ.get("IPC_LOCAL_BASH", "").strip()
    if configured:
        candidates.append(Path(configured))

    git = shutil.which("git")
    if git:
        git_path = Path(git).resolve()
        # Typical layout: <Git>\\cmd\\git.exe -> <Git>\\bin\\bash.exe.
        if len(git_path.parents) >= 2:
            candidates.append(git_path.parents[1] / "bin" / "bash.exe")

    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _windows_paths_for_bash(command: str) -> str:
    """Convert unquoted ``D:\\path`` forms produced by Members to ``/d/path``.

    This intentionally targets only absolute drive paths.  It leaves normal
    command text untouched and avoids rewriting arbitrary backslash escapes.
    """
    def replace_path(match: re.Match[str]) -> str:
        return "/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/")

    return re.sub(
        r"(?<![A-Za-z0-9_])([A-Za-z]):\\([^\s'\"`|&;()<>]*)",
        replace_path,
        command,
    )


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill the process and every descendant that shares its pipes.

    Killing only the direct child leaves grandchildren that inherited the
    stdout/stderr handles alive; the follow-up pipe read then blocks until the
    grandchild exits (observed with shell loops and interactive ``unzip`` on
    Windows).  Kill the whole tree so the read terminates.
    """

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    name: str

    def start(self) -> None: ...
    def exec(self, command: str, timeout: int = 60) -> ExecResult: ...
    def write_file(self, rel_path: str, content: str) -> None: ...
    def read_file(self, rel_path: str) -> str | None: ...
    def stop(self) -> None: ...


class LocalSandbox:
    """Subprocess-backed sandbox rooted at an isolated workspace directory.

    Not a security boundary - it is for development/testing and to let the full
    orchestration run without Docker. Commands run with cwd set to the workspace
    so members can't trivially see each other's files.
    """

    def __init__(self, name: str, workspace: str | Path, env: dict[str, str] | None = None):
        self.name = name
        self.workspace = Path(workspace)
        self.env = env or {}
        self._lock = threading.Lock()
        self._started = False
        self._webui_keys: set[tuple[str, str]] = set()
        self._jobs: dict[str, subprocess.Popen] = {}

    def start(self) -> None:
        with self._lock:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self._started = True

    def exec(self, command: str, timeout: int = 60) -> ExecResult:
        if not self._started:
            self.start()
        full_env = {**os.environ, **self.env}
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        full_env.setdefault("PYTHONUTF8", "1")
        bash = _git_bash_executable()
        args: str | list[str] = command
        use_shell = True
        if bash is not None:
            args = [str(bash), "-lc", _windows_paths_for_bash(command)]
            use_shell = False
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            args,
            shell=use_shell,
            cwd=str(self.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
            **popen_kwargs,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return ExecResult(int(proc.returncode or 0), out or "", err or "")
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return ExecResult(124, out or "", err or "", timed_out=True)

    def write_file(self, rel_path: str, content: str) -> None:
        target = self._safe_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # ---- detached jobs ----
    #
    # The Docker backend is the real one; this mirrors its contract so dev-mode
    # runs and tests exercise the same job lifecycle. The handle is again files
    # on disk plus a PID, not a Popen object, so a poll works from a different
    # process after a restart.

    def exec_detached(self, job_id: str, command: str) -> int:
        directory = self._job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        full_env = {**os.environ, **self.env}
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        full_env.setdefault("PYTHONUTF8", "1")
        bash = _git_bash_executable()
        args: str | list[str] = command
        use_shell = True
        if bash is not None:
            args = [str(bash), "-lc", _windows_paths_for_bash(command)]
            use_shell = False
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        stdout = (directory / "stdout").open("wb")
        stderr = (directory / "stderr").open("wb")
        try:
            proc = subprocess.Popen(
                args,
                shell=use_shell,
                cwd=str(self.workspace),
                stdout=stdout,
                stderr=stderr,
                env=full_env,
                **popen_kwargs,
            )
        finally:
            stdout.close()
            stderr.close()
        (directory / "pid").write_text(str(proc.pid), encoding="utf-8")
        with self._lock:
            self._jobs[job_id] = proc
        return proc.pid

    def job_state(self, job_id: str) -> dict:
        directory = self._job_dir(job_id)
        pid_file = directory / "pid"
        pid = int(pid_file.read_text(encoding="utf-8").strip()) if pid_file.is_file() else None
        with self._lock:
            proc = self._jobs.get(job_id)
        status_file = directory / "status"
        exit_code: int | None = None
        if proc is not None:
            code = proc.poll()
            if code is not None:
                exit_code = int(code)
                # Persist it so a later poll - possibly from another process -
                # reads the same answer instead of reporting the job lost.
                status_file.write_text(str(exit_code), encoding="utf-8")
        elif status_file.is_file():
            text = status_file.read_text(encoding="utf-8").strip()
            exit_code = int(text) if text.lstrip("-").isdigit() else None
        if exit_code is not None:
            state = "exited"
        elif proc is not None or (pid is not None and self._pid_alive(pid)):
            state = "running"
        else:
            # Unknown is not success: see TaskSandbox.job_state.
            state = "lost"
        # Note: the pid-recycling guard TaskSandbox needs does not apply here.
        # This sandbox is development-only and single-process, so the live
        # ``Popen`` handle is the identity; the pid path is only a fallback for
        # a poll after a restart, which also loses the handle and so resolves
        # to 'lost' rather than to another process.
        return {
            "state": state,
            "exit_code": exit_code,
            "pid": pid,
            "stdout_bytes": self._size(directory / "stdout"),
            "stderr_bytes": self._size(directory / "stderr"),
        }

    def job_output(
        self, job_id: str, stream: str = "stdout", *, offset: int = 0,
        limit: int = 32_768,
    ) -> tuple[str, int]:
        if stream not in {"stdout", "stderr"}:
            raise ValueError("stream must be stdout or stderr")
        path = self._job_dir(job_id) / stream
        if not path.is_file():
            return "", max(0, offset)
        start = max(0, offset)
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(max(1, limit))
        return chunk.decode("utf-8", errors="replace"), start + len(chunk)

    def job_cancel(self, job_id: str) -> bool:
        with self._lock:
            proc = self._jobs.get(job_id)
        if proc is not None:
            if proc.poll() is None:
                _kill_process_tree(proc)
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
            code = proc.poll()
            (self._job_dir(job_id) / "status").write_text(
                str(int(code if code is not None else -1)), encoding="utf-8"
            )
            return True
        pid_file = self._job_dir(job_id) / "pid"
        if not pid_file.is_file():
            return False
        pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        if not pid:
            return False
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True, check=False,
                )
            else:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    def _job_dir(self, job_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id).strip("._") or "job"
        return self.workspace / ".ipc" / "jobs" / safe

    @staticmethod
    def _size(path: Path) -> int:
        return path.stat().st_size if path.is_file() else 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, check=False,
            )
            return str(pid) in (result.stdout or "")
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False
        except PermissionError:
            return True
        return True
    def read_file(self, rel_path: str) -> str | None:
        target = self._safe_path(rel_path)
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        for project_id, member in list(self._webui_keys):
            webui_proxy_manager.close_member(project_id, member)
        self._webui_keys.clear()
        self._started = False

    def expose_webui(self, project_id: str, member: str, port: int) -> str:
        handle = webui_proxy_manager.register(project_id, member, "127.0.0.1", port)
        self._webui_keys.add((project_id, member))
        return handle.url

    def _safe_path(self, rel_path: str) -> Path:
        p = (self.workspace / rel_path).resolve()
        ws = self.workspace.resolve()
        if os.path.commonpath([str(p), str(ws)]) != str(ws):
            raise ValueError(f"path escapes workspace: {rel_path}")
        return p
