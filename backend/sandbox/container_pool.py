from __future__ import annotations

from contextlib import suppress
import re
import threading
from pathlib import Path

from backend.sandbox.resource_limiter import TaskSlotLimiter
from backend.sandbox.sandbox import LocalSandbox, Sandbox

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str) -> str:
    text = _SAFE_SEGMENT.sub("_", value).strip("._")
    return text or "workspace"


class ContainerPool:
    """Owns one sandbox per CTF task.

    In docker mode a task maps to a single ``TaskSandbox`` (one container per
    project) whose ``member_view`` gives each Member an isolated working
    directory inside the shared container. In local mode each Member still gets
    its own ``LocalSandbox`` directory.
    """

    def __init__(
        self,
        backend: str = "local",
        workspace_root: str | Path = "projects",
        image: str = "ipc-task:latest",
        limiter: TaskSlotLimiter | None = None,
        network: bool = True,
    ):
        self.backend = backend
        self.workspace_root = Path(workspace_root)
        self.image = image
        self.limiter = limiter or TaskSlotLimiter()
        self.network = network
        # docker: one TaskSandbox per project. local: unused (per-member below).
        self._tasks: dict[str, object] = {}
        # per-(project, member) sandbox view, cached for identity stability.
        self._sandboxes: dict[tuple[str, str], Sandbox] = {}
        self._lock = threading.Lock()

    def _key(self, project_id: str, member: str) -> tuple[str, str]:
        return (project_id, member)

    def get(self, project_id: str, member: str, env: dict[str, str] | None = None) -> Sandbox:
        key = self._key(project_id, member)
        try:
            with self._lock:
                sb = self._sandboxes.get(key)
                if sb is not None:
                    created = False
                else:
                    sb = self._create(project_id, member, env)
                    self._sandboxes[key] = sb
                    created = True
            sb.start()
        except Exception:
            with self._lock:
                if "sb" in locals() and created and self._sandboxes.get(key) is sb:
                    self._sandboxes.pop(key, None)
                if not any(pid == project_id for pid, _ in self._sandboxes):
                    self._tasks.pop(project_id, None)
            raise
        return sb

    def preflight(self, project_id: str, env: dict[str, str] | None = None) -> None:
        """Start the task container before any model work consumes solver budget."""

        if self.backend != "docker":
            return
        with self._lock:
            task = self._tasks.get(project_id)
            if task is None:
                task = self._new_task(project_id, env)
                self._tasks[project_id] = task
                created = True
            else:
                created = False
        try:
            task.preflight()
            task.start()
        except Exception:
            with self._lock:
                if created and self._tasks.get(project_id) is task:
                    self._tasks.pop(project_id, None)
            with suppress(Exception):
                task.stop()
            raise

    def _create(self, project_id: str, member: str, env: dict[str, str] | None) -> Sandbox:
        if self.backend == "docker":
            task = self._tasks.get(project_id)
            if task is None:
                task = self._new_task(project_id, env)
                self._tasks[project_id] = task
            return task.member_view(member)
        ws = self.workspace_root / project_id / "sandbox" / member
        return LocalSandbox(name=f"{project_id}-{member}", workspace=ws, env=env)

    def _new_task(self, project_id: str, env: dict[str, str] | None):
        from backend.sandbox.task_sandbox import TaskSandbox

        return TaskSandbox(
            project_id=project_id,
            image=self.image,
            env=env,
            network=self.network,
            attachments_dir=self.workspace_root / project_id / "attachments",
        )

    def stop_project(self, project_id: str) -> None:
        with self._lock:
            keys = [k for k in self._sandboxes if k[0] == project_id]
            sandboxes = [self._sandboxes.pop(k) for k in keys]
            task = self._tasks.pop(project_id, None)
        for sb in sandboxes:
            sb.stop()
        if task is not None:
            task.stop()

    def stop_all(self) -> None:
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for sb in sandboxes:
            sb.stop()
        for task in tasks:
            task.stop()

    def active_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._sandboxes)

    def active_projects(self) -> list[str]:
        with self._lock:
            projects = {k[0] for k in self._sandboxes} | set(self._tasks)
            return sorted(projects)
