from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.redaction import redact_object
from backend.core.replay import build_timeline, export_yaml
from backend.core.state import AppState

router = APIRouter(tags=["graph"])



# FastAPI resolves request values from these reusable declaration markers.
_GET_STATE_DEPENDENCY = Depends(get_state)

@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml", state: AppState = _GET_STATE_DEPENDENCY):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "format must be yaml or timeline")
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise HTTPException(404, "Project not found")
    public_detail = detail.__class__.model_validate(
        redact_object(detail.model_dump(mode="json"), detail.project.flag)
    )
    text = export_yaml(public_detail) if format == "yaml" else _timeline_text(public_detail)
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
def replay_timeline(project_id: str, state: AppState = _GET_STATE_DEPENDENCY):
    """Ordered event frames for the UI replay (Seed.md 重放)."""
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise HTTPException(404, "Project not found")
    public_detail = detail.__class__.model_validate(
        redact_object(detail.model_dump(mode="json"), detail.project.flag)
    )
    return {"events": build_timeline(public_detail)}
