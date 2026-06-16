from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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

    def start(self) -> None:
        with self._lock:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self._started = True

    def exec(self, command: str, timeout: int = 60) -> ExecResult:
        if not self._started:
            self.start()
        full_env = {**os.environ, **self.env}
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
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
        self._started = False

    def _safe_path(self, rel_path: str) -> Path:
        p = (self.workspace / rel_path).resolve()
        ws = self.workspace.resolve()
        if os.path.commonpath([str(p), str(ws)]) != str(ws):
            raise ValueError(f"path escapes workspace: {rel_path}")
        return p
