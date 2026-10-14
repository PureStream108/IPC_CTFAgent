from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TaskSlotLimiter:
    """Track tasks and gate work admitted by the standalone scheduler.

    Competition work has already passed its own local/remote capacity and
    global Member-seat checks. Track it for cleanup without charging a second
    standalone slot.
    """

    max_concurrent_tasks: int = 5
    total_cpu: int = 4

    _active: set[str] = field(default_factory=set)
    _coordinated: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def can_admit(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self._active:
                return True
            return len(self._active - self._coordinated) < self.max_concurrent_tasks

    def acquire(self, project_id: str, *, externally_scheduled: bool = False) -> bool:
        """Reserve a task slot. Idempotent for a project that already holds one."""
        with self._lock:
            if project_id in self._active:
                return True
            if not externally_scheduled and len(self._active - self._coordinated) >= self.max_concurrent_tasks:
                return False
            self._active.add(project_id)
            if externally_scheduled:
                self._coordinated.add(project_id)
            return True

    def release(self, project_id: str) -> None:
        with self._lock:
            self._active.discard(project_id)
            self._coordinated.discard(project_id)

    def active_tasks(self) -> list[str]:
        with self._lock:
            return sorted(self._active)

    def update_limits(self, *, max_concurrent_tasks: int | None = None, total_cpu: int | None = None) -> None:
        """Apply configuration changes without interrupting admitted tasks."""
        with self._lock:
            if max_concurrent_tasks is not None:
                if int(max_concurrent_tasks) < 0:
                    raise ValueError("max_concurrent_tasks must be non-negative")
                self.max_concurrent_tasks = int(max_concurrent_tasks)
            if total_cpu is not None:
                if int(total_cpu) < 0:
                    raise ValueError("total_cpu must be non-negative")
                self.total_cpu = int(total_cpu)
