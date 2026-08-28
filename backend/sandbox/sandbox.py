from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
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
        try:
            proc = subprocess.run(
                args,
                shell=use_shell,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=full_env,
            )
            return ExecResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
            return ExecResult(124, out, err, timed_out=True)

    def write_file(self, rel_path: str, content: str) -> None:
        target = self._safe_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

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
