from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState

router = APIRouter(tags=["logs"])
LOG_GROUPS = (
    ("project", "project_log"),
    ("llm", "llm_log"),
    ("tool", "tool_log"),
    ("memory", "memory_log"),
)


class LogToggle(BaseModel):
    enabled: bool


@router.get("/logs/status")
def log_status(state: AppState = Depends(get_state)):
    return {"enabled": state.logger.enabled}


@router.put("/logs/status")
def set_log_status(body: LogToggle, state: AppState = Depends(get_state)):
    state.logger.set_enabled(body.enabled)
    state.config.log_enabled = body.enabled
    state.save_config()
    return {"enabled": state.logger.enabled}


@router.get("/logs/projects")
def read_project_logs(limit: int = 500, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    logs = []
    for project in projects:
        item = {
            "project_id": project.id,
            "title": project.title,
            "status": project.status,
        }
        for kind, key in LOG_GROUPS:
            item[key] = {
                "filename": project.log_filename or f"{project.id}.json",
                "entries": state.logger.read_log(kind, project.id, limit),
            }
        logs.append(item)
    return {
        "logs": logs
    }


@router.post("/logs/derive")
def derive_project_logs(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    target = state.log_export_dir
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[str]] = {}
    for kind, _ in LOG_GROUPS:
        folder = target / state.logger.KINDS[kind]
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob("*.json"):
            stale.unlink()
        files[state.logger.KINDS[kind]] = []
        for project in projects:
            filename = project.log_filename or f"{project.id}.json"
            entries = state.logger.read_log(kind, project.id, None)
            (folder / filename).write_text(
                json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            files[state.logger.KINDS[kind]].append(filename)
    return {"dir": str(target), "files": files}


@router.get("/logs/{project_id}")
def read_logs(project_id: str, kind: str = "project", limit: int = 500, state: AppState = Depends(get_state)):
    return {"entries": state.logger.read_log(kind, project_id, limit)}
