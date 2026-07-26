from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class TaskSlotLimiter:
    """Concurrency gate for CTF tasks.

    Each running CTF task (project) occupies one slot. Memory is no longer
    reserved per agent - a task owns a single Docker container shared by all of
    its Members, and that container is not memory-capped. The only global limit
    is how many tasks may run at once.
    """

    max_concurrent_tasks: int = 5
    total_cpu: int = 4

    _active: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def can_admit(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self._active:
                return True
            return len(self._active) < self.max_concurrent_tasks

    def acquire(self, project_id: str) -> bool:
        """Reserve a task slot. Idempotent for a project that already holds one."""
        with self._lock:
            if project_id in self._active:
                return True
            if len(self._active) >= self.max_concurrent_tasks:
                return False
            self._active.add(project_id)
            return True

    def release(self, project_id: str) -> None:
        with self._lock:
            self._active.discard(project_id)

    def active_tasks(self) -> list[str]:
        with self._lock:
            return sorted(self._active)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def reset(self) -> None:
        with self._lock:
            self._active.clear()
