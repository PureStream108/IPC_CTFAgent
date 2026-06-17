from __future__ import annotations

from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.resource_limiter import ResourceLimiter


class ResourceManager:
    def __init__(self, limiter: ResourceLimiter, pool: ContainerPool):
        self.limiter = limiter
        self.pool = pool

    def can_admit_member(self) -> bool:
        if self.limiter.can_admit():
            return True
        if not self.pool.active_keys():
            self.limiter.reset()
            return self.limiter.can_admit()
        return False

    def reclaim_orphaned_projects(self, active_project_ids: set[str]) -> list[str]:
        orphaned = sorted({project_id for project_id, _ in self.pool.active_keys() if project_id not in active_project_ids})
        for project_id in orphaned:
            self.pool.stop_project(project_id)
        if orphaned and not self.pool.active_keys():
            self.limiter.reset()
        return orphaned

    def sandbox_for(self, project_id: str, member: str, env: dict | None = None):
        return self.pool.get(project_id, member, env)

    def release_project(self, project_id: str) -> None:
        self.pool.stop_project(project_id)
