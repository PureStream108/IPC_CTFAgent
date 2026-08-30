from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.flag_gate import validate_flag
from backend.core.state import AppState

router = APIRouter(prefix="/api/flags", tags=["flags"])

# Latest-verdict subquery shared by the list and detail endpoints.
_VERDICT_SQL = (
    "(SELECT s.status FROM submissions s WHERE s.project_id = p.id "
    "ORDER BY s.updated_at DESC, s.rowid DESC LIMIT 1)"
)


class FlagRecord(BaseModel):
    project_id: str
    external_id: str | None = None
    title: str
    category: str
    status: str
    flag: str | None = None
    found_at: str | None = None
    submitted: bool
    verdict: str | None = None


def _record(row) -> FlagRecord:
    verdict = row["verdict"]
    # ``submitted`` reflects the platform submission ledger; legacy projects
    # completed before the ledger existed fall back to the completion
    # broadcast so their display does not regress.
    submitted = verdict is not None or bool(row["broadcast_done"])
    return FlagRecord(
        project_id=row["id"],
        external_id=row["external_id"],
        title=row["title"],
        category=row["category"],
        status=row["status"],
        flag=row["flag"],
        found_at=row["updated_at"] if row["flag"] is not None else None,
        submitted=submitted,
        verdict=verdict,
    )


@router.get("", response_model=list[FlagRecord])
def list_flags(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT p.*,
                {_VERDICT_SQL} AS verdict,
                EXISTS(SELECT 1 FROM broadcasts b WHERE b.project_id = p.id) AS broadcast_done
            FROM projects p
            ORDER BY p.created_at
            """
        ).fetchall()
    return [_record(row) for row in rows]


@router.get("/{project_id}", response_model=FlagRecord)
def get_flag(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT p.*,
                {_VERDICT_SQL} AS verdict,
                EXISTS(SELECT 1 FROM broadcasts b WHERE b.project_id = p.id) AS broadcast_done
            FROM projects p
            WHERE p.id = ?
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return _record(row)


class SubmitRequest(BaseModel):
    flag: str | None = None  # defaults to the project's recorded flag


@router.post("/{project_id}/submit")
def submit_flag(project_id: str, body: SubmitRequest, state: AppState = Depends(get_state)):
    """Manually submit a flag through the same platform-verdict channel.

    The result is recorded in the submissions ledger and applied to the
    project (accepted → completed, rejected → feedback + reopen), so a manual
    resubmit can no longer drift from local state.
    """
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        external_id = (row["external_id"] or "").strip()
        recorded_flag = row["flag"]
    if not external_id:
        raise HTTPException(400, "Project has no platform external_id")
    flag = (body.flag or recorded_flag or "").strip()
    if not flag:
        raise HTTPException(400, "No flag to submit")
    reason = validate_flag(flag, state.config.runtime.flag_pattern)
    if reason is not None:
        raise HTTPException(400, f"flag rejected by local gate: {reason}")
    if state.orchestrator is None:
        raise HTTPException(503, "Orchestrator not running")
    with state.db.connect() as conn:
        existing = graph_store.get_submission(conn, project_id, flag)
    if existing is not None and existing["status"] == "solved":
        return {"solved": True, "verdict": existing["verdict"], "dedup": True}
    with state.db.connect() as conn:
        graph_store.record_submission(conn, project_id, flag)
    result = state.orchestrator.verdicts.submit_and_apply(
        project_id, flag, retry_backoff=False
    )
    if result is None:
        raise HTTPException(503, "Platform unavailable or submit quota exhausted; try later")
    return result
