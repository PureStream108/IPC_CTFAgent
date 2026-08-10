from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.core.state import AppState

router = APIRouter(prefix="/api/flags", tags=["flags"])


class FlagRecord(BaseModel):
    project_id: str
    external_id: str | None = None
    title: str
    category: str
    status: str
    postprocess_status: str
    flag: str | None = None
    found_at: str | None = None
    verified_at: str | None = None
    submitted: bool


def _record(row) -> FlagRecord:
    return FlagRecord(
        project_id=row["id"],
        external_id=row["external_id"],
        title=row["title"],
        category=row["category"],
        status=row["status"],
        postprocess_status=row["postprocess_status"],
        flag=row["flag"],
        found_at=row["flag_verified_at"] or (row["updated_at"] if row["flag"] is not None else None),
        verified_at=row["flag_verified_at"],
        submitted=bool(row["submitted"]),
    )


@router.get("", response_model=list[FlagRecord])
def list_flags(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                EXISTS(SELECT 1 FROM broadcasts b WHERE b.project_id = p.id) AS submitted
            FROM projects p
            ORDER BY p.created_at
            """
        ).fetchall()
    return [_record(row) for row in rows]


@router.get("/{project_id}", response_model=FlagRecord)
def get_flag(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = conn.execute(
            """
            SELECT p.*,
                EXISTS(SELECT 1 FROM broadcasts b WHERE b.project_id = p.id) AS submitted
            FROM projects p
            WHERE p.id = %s
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return _record(row)
