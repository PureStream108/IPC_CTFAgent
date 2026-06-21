from __future__ import annotations

import re
import threading
from pathlib import Path

from backend.sandbox.resource_limiter import ResourceLimiter
from backend.sandbox.sandbox import LocalSandbox, Sandbox

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str) -> str:
    text = _SAFE_SEGMENT.sub("_", value).strip("._")
    return text or "workspace"


class ContainerPool:
    def __init__(
        self,
        backend: str = "local",
        workspace_root: str | Path = "projects",
        image: str = "ipc-member:latest",
        limiter: ResourceLimiter | None = None,
        network: bool = True,
    ):
        self.backend = backend
        self.workspace_root = Path(workspace_root)
        self.image = image
        self.limiter = limiter or ResourceLimiter()
        self.network = network
        self._sandboxes: dict[tuple[str, str], Sandbox] = {}
        self._lock = threading.Lock()

    def _key(self, project_id: str, member: str) -> tuple[str, str]:
        return (project_id, member)

    def get(self, project_id: str, member: str, env: dict[str, str] | None = None) -> Sandbox:
        key = self._key(project_id, member)
        with self._lock:
            sb = self._sandboxes.get(key)
            if sb is not None:
                created = False
            else:
                sb = self._create(project_id, member, env)
                self._sandboxes[key] = sb
                created = True
        try:
            sb.start()
        except Exception:
            with self._lock:
                if created and self._sandboxes.get(key) is sb:
                    self._sandboxes.pop(key, None)
            raise
        return sb

    def _create(self, project_id: str, member: str, env: dict[str, str] | None) -> Sandbox:
        name = f"{project_id}-{member}"
        if self.backend == "docker":
            from backend.sandbox.docker_manager import DockerSandbox

            return DockerSandbox(
                name=name,
                image=self.image,
                env=env,
                memory_gb=self.limiter.per_agent_memory_gb,
                network=self.network,
                limiter=self.limiter,
                workdir=f"/workspace/{_safe_segment(project_id)}/{_safe_segment(member)}",
                attachments_dir=self.workspace_root / project_id / "attachments",
            )
        ws = self.workspace_root / project_id / "sandbox" / member
        return LocalSandbox(name=name, workspace=ws, env=env)

    def stop_member(self, project_id: str, member: str) -> None:
        key = self._key(project_id, member)
        with self._lock:
            sb = self._sandboxes.pop(key, None)
        if sb is not None:
            sb.stop()

    def stop_project(self, project_id: str) -> None:
        with self._lock:
            keys = [k for k in self._sandboxes if k[0] == project_id]
            sandboxes = [self._sandboxes.pop(k) for k in keys]
        for sb in sandboxes:
            sb.stop()

    def stop_all(self) -> None:
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
        for sb in sandboxes:
            sb.stop()

    def active_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._sandboxes)
