from __future__ import annotations

from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.resource_limiter import TaskSlotLimiter


class ResourceManager:
    def __init__(self, limiter: TaskSlotLimiter, pool: ContainerPool):
        self.limiter = limiter
        self.pool = pool

    def can_admit_task(self, project_id: str) -> bool:
        return self.limiter.can_admit(project_id)

    def acquire_task(self, project_id: str) -> bool:
        return self.limiter.acquire(project_id)

    def reclaim_orphaned_projects(self, active_project_ids: set[str]) -> list[str]:
        orphaned = sorted(pid for pid in self.pool.active_projects() if pid not in active_project_ids)
        for project_id in orphaned:
            self.pool.stop_project(project_id)
            self.limiter.release(project_id)
        return orphaned

    def sandbox_for(self, project_id: str, member: str, env: dict | None = None):
        return self.pool.get(project_id, member, env)

    def preflight_project(self, project_id: str, env: dict | None = None) -> None:
        self.pool.preflight(project_id, env)

    def release_project(self, project_id: str) -> None:
        self.pool.stop_project(project_id)
        self.limiter.release(project_id)
