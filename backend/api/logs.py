from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_state
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


@router.get("/logs/{project_id}")
def read_logs(project_id: str, kind: str = "project", limit: int = 500, state: AppState = Depends(get_state)):
    return {"entries": state.logger.read_log(kind, project_id, limit)}
