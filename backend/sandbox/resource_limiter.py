from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class ResourceLimiter:
    total_cpu: int = 4
    total_memory_gb: int = 20
    total_disk_gb: int = 25
    per_agent_memory_gb: int = 5

    _reserved_memory: float = 0.0
    _reserved: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def can_admit(self, memory_gb: float | None = None) -> bool:
        mem = memory_gb if memory_gb is not None else self.per_agent_memory_gb
        if mem > self.per_agent_memory_gb:
            return False
        with self._lock:
            return self._reserved_memory + mem <= self.total_memory_gb

    def reserve(self, agent: str, memory_gb: float | None = None) -> bool:
        mem = memory_gb if memory_gb is not None else self.per_agent_memory_gb
        with self._lock:
            if mem > self.per_agent_memory_gb:
                return False
            if self._reserved_memory + mem > self.total_memory_gb:
                return False
            self._reserved[agent] = mem
            self._reserved_memory += mem
            return True

    def release(self, agent: str) -> None:
        with self._lock:
            mem = self._reserved.pop(agent, 0.0)
            self._reserved_memory -= mem
            if self._reserved_memory < 0:
                self._reserved_memory = 0.0

    @property
    def reserved_memory_gb(self) -> float:
        with self._lock:
            return self._reserved_memory

    def docker_limits(self, memory_gb: float | None = None) -> dict:
        """Return Docker run kwargs enforcing the per-agent cap."""
        mem = memory_gb if memory_gb is not None else self.per_agent_memory_gb
        return {
            "mem_limit": f"{int(mem)}g",
            "nano_cpus": int(self.total_cpu * 1e9),
        }
