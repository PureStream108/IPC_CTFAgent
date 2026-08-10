from __future__ import annotations

from backend.persistence.database import Database, PostgresDatabase


PROJECT_STATES = (
    "created",
    "running",
    "flag_found",
    "solved",
    "timeout",
    "infra_error",
    "failed",
    "stopped",
)

__all__ = ["Database", "PostgresDatabase", "PROJECT_STATES"]
