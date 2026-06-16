from __future__ import annotations

from backend.blackboard import graph_store

# allowed transitions (status maps to blackboard project.status)
TRANSITIONS = {
    "created": {"running", "stopped"},
    "running": {"flag_found", "stopped"},
    "flag_found": {"wp_writing", "stopped"},
    "wp_writing": {"memory_writing", "stopped"},
    "memory_writing": {"completed", "stopped"},
    "completed": set(),
    "stopped": {"running"},
}


class LifecycleError(RuntimeError):
    pass


class Lifecycle:
    def __init__(self, db):
        self.db = db

    def status(self, project_id: str) -> str | None:
        with self.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            return row["status"] if row else None

    def can_transition(self, current: str, target: str) -> bool:
        return target in TRANSITIONS.get(current, set())

    def transition(self, project_id: str, target: str) -> str:
        with self.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row is None:
                raise LifecycleError(f"no project {project_id}")
            current = row["status"]
            if current == target:
                return target
            if target not in TRANSITIONS.get(current, set()):
                raise LifecycleError(f"illegal transition {current} -> {target}")
            graph_store.set_status(conn, project_id, target)
        return target
