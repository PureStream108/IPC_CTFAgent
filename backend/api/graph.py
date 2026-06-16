from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState
from backend.core.replay import build_timeline, export_yaml

router = APIRouter(tags=["graph"])


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml", state: AppState = Depends(get_state)):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "format must be yaml or timeline")
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise HTTPException(404, "Project not found")
    text = export_yaml(detail) if format == "yaml" else _timeline_text(detail)
    return Response(content=text, media_type="text/plain")


def _timeline_text(detail) -> str:
    events = build_timeline(detail)
    lines = []
    for ev in events:
        lines.append(f"[{ev['ts']}] {ev['kind'].upper()} {ev.get('label','')}".rstrip())
        if ev.get("detail"):
            lines.append(f"  {ev['detail']}")
    return "\n".join(lines) + "\n"


@router.get("/projects/{project_id}/replay")
def replay_timeline(project_id: str, state: AppState = Depends(get_state)):
    """Ordered event frames for the UI replay (Seed.md 重放)."""
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise HTTPException(404, "Project not found")
    return {"events": build_timeline(detail)}
