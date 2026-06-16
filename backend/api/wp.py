from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState

router = APIRouter(tags=["wp"])


@router.get("/projects/{project_id}/wp")
def get_wp(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    wp_path = row["wp_path"]
    if not wp_path:
        raise HTTPException(404, "No writeup yet")
    from pathlib import Path

    p = Path(wp_path)
    if not p.exists():
        raise HTTPException(404, "Writeup file missing")
    return Response(content=p.read_text(encoding="utf-8"), media_type="text/markdown")


@router.get("/wp")
def list_wp(state: AppState = Depends(get_state)):
    files = sorted(state.wp_dir.glob("*.md"))
    return {"writeups": [f.name for f in files]}
