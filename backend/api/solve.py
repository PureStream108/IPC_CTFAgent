from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.deps import get_state
from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.models import (
    Broadcast,
    CompleteRequest,
    Intent,
    Report,
    ReportRequest,
)
from backend.core.state import AppState
from backend.core.ipc import FlagConflictError, submit_flag_candidate
from backend.core.postprocess_store import enqueue_postprocess

router = APIRouter(tags=["solve"])


@router.post("/projects/{project_id}/start")
def start_solving(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] == "solved":
            raise HTTPException(409, "Project already solved")
    errors = state.config.startup_errors()
    if errors:
        raise HTTPException(400, "; ".join(errors))
    if state.orchestrator is None:
        raise HTTPException(503, "Orchestrator not running")
    try:
        return state.orchestrator.start_project_async(project_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/projects/{project_id}/reopen")
def reopen_project(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] == "solved":
            raise HTTPException(409, "Solved projects are immutable; create a new project to retry")
        raise HTTPException(409, "Only terminal failed projects can be resumed")


@router.post("/projects/{project_id}/stop")
def stop_solving(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] == "solved":
            raise HTTPException(409, "Solved projects cannot be stopped")
        graph_store.set_status(conn, project_id, "stopped")
        conn.execute(
            "UPDATE intents SET worker = NULL, lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL, last_heartbeat_at = NULL "
            "WHERE project_id = %s AND concluded_at IS NULL",
            (project_id,),
        )
        graph_store.clear_reason(conn, project_id)
    if state.orchestrator is not None:
        state.orchestrator.stop_project(project_id)
    state.logger.project("stopped", project_id)
    return {"status": "stopped", "project_id": project_id}


@router.post("/projects/stop-all")
def stop_all(state: AppState = Depends(get_state)):
    stopped = []
    with state.db.connect() as conn:
        rows = conn.execute(
            "SELECT id FROM projects WHERE status NOT IN ('solved','stopped')"
        ).fetchall()
        ids = [r["id"] for r in rows]
    for pid in ids:
        try:
            stop_solving(pid, state)
            stopped.append(pid)
        except HTTPException:
            pass
    return {"stopped": stopped}


@router.post("/projects/{project_id}/resume")
def resume_solving(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] not in ("stopped", "timeout", "infra_error", "failed"):
            raise HTTPException(409, "Only stopped or failed projects can resume")
        if row["status"] != "stopped":
            graph_store.set_status(conn, project_id, "stopped")
    errors = state.config.startup_errors()
    if errors:
        raise HTTPException(400, "; ".join(errors))
    if state.orchestrator is None:
        raise HTTPException(503, "Orchestrator not running")
    try:
        return state.orchestrator.start_project_async(project_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/projects/{project_id}/reports", response_model=Report, status_code=201)
def submit_report(project_id: str, body: ReportRequest, state: AppState = Depends(get_state)):
    """A Member submits a difficulty report to Diamond (Seed.md 角色联动)."""
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
        report = graph_store.create_report(
            conn, project_id, body.member, body.progress, body.difficulty,
            body.node_id, body.steps, body.directions, body.knowledge,
        )
        # draw the report line Member -> Diamond
        graph_store.add_link(conn, project_id, body.member, "diamond", "report")
    state.logger.project(
        "difficulty_report", project_id, member=body.member,
        difficulty=body.difficulty, directions=body.directions,
    )
    # Let Diamond react (assign more members) if orchestrator is live.
    if state.orchestrator is not None:
        state.orchestrator.handle_report(project_id, report)
    return report


@router.post("/projects/{project_id}/complete", response_model=Intent)
def complete_project(project_id: str, body: CompleteRequest, state: AppState = Depends(get_state)):
    """Mark a flag found (IPC verification entry). Creates the goal edge."""
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        for fid in body.from_:
            if not node_store.fact_exists(conn, project_id, fid):
                raise HTTPException(404, f"Fact {fid} not found")
        if "goal" in body.from_:
            raise HTTPException(400, "goal cannot be used in from")
        if not body.flag:
            raise HTTPException(400, "A flag is required to complete a project")
        try:
            intent = edge_store.complete_goal_intent(
                conn, project_id, body.from_, body.description, body.worker, body.worker
            )
            candidate = submit_flag_candidate(
                conn,
                project_id,
                body.flag,
                source=f"api:{body.worker}",
            )
            if candidate["mode"] == "verified":
                enqueue_postprocess(conn, project_id)
            graph_store.add_link(conn, project_id, f"fact:{body.from_[0]}", "flag", "flag")
            intent_model = edge_store.intent_to_model(
                conn, edge_store.get_intent(conn, project_id, intent.id), project_id
            )
        except FlagConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    state.logger.project("flag_found", project_id, worker=body.worker, flag=body.flag)
    if state.orchestrator is not None:
        state.orchestrator.on_flag_found(project_id)
    return intent_model


@router.get("/broadcasts", response_model=list[Broadcast])
def list_broadcasts(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        return graph_store.list_broadcasts(conn)
