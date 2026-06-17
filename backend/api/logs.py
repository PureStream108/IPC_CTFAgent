from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState

router = APIRouter(tags=["logs"])


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
    return {
        "logs": [
            {
                "project_id": p.id,
                "title": p.title,
                "status": p.status,
                "filename": p.log_filename or f"{p.id}.json",
                "entries": state.logger.read_log("project", p.id, limit),
            }
            for p in projects
        ]
    }


@router.post("/logs/derive")
def derive_project_logs(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    target = state.log_export_dir
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob("*.json"):
        stale.unlink()
    files: list[str] = []
    for p in projects:
        filename = p.log_filename or f"{p.id}.json"
        entries = state.logger.read_log("project", p.id, None)
        (target / filename).write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        files.append(filename)
    return {"dir": str(target), "files": files}


@router.get("/logs/{project_id}")
def read_logs(project_id: str, kind: str = "project", limit: int = 500, state: AppState = Depends(get_state)):
    return {"entries": state.logger.read_log(kind, project_id, limit)}
