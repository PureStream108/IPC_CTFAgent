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

router = APIRouter(tags=["solve"])


@router.post("/projects/{project_id}/start")
def start_solving(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] in ("completed",):
            raise HTTPException(409, "Project already completed")
    errors = state.config.startup_errors()
    if errors:
        raise HTTPException(400, "; ".join(errors))
    if state.orchestrator is None:
        raise HTTPException(503, "Orchestrator not running")
    state.orchestrator.start_project(project_id)
    return {"status": "running", "project_id": project_id}


@router.post("/projects/{project_id}/reopen")
def reopen_project(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] != "completed":
            raise HTTPException(409, "Only completed projects can be reopened")
        graph_store.set_status(conn, project_id, "stopped")
        graph_store.clear_reason(conn, project_id)
    state.logger.project("reopened", project_id)
    return {"status": "stopped", "project_id": project_id}


@router.post("/projects/{project_id}/stop")
def stop_solving(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        if row["status"] == "completed":
            raise HTTPException(409, "Completed projects cannot be stopped")
        graph_store.set_status(conn, project_id, "stopped")
        # release open claims + reason lease
        conn.execute(
            "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
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
            "SELECT id FROM projects WHERE status NOT IN ('completed','stopped')"
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
        if row["status"] != "stopped":
            raise HTTPException(409, "Only stopped projects can resume")
    if state.orchestrator is not None:
        state.orchestrator.resume_project(project_id)
    else:
        with state.db.connect() as conn:
            graph_store.set_status(conn, project_id, "running")
    return {"status": "running", "project_id": project_id}


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
        if row["status"] in ("completed",):
            raise HTTPException(409, "Project already completed")
        for fid in body.from_:
            if not node_store.fact_exists(conn, project_id, fid):
                raise HTTPException(404, f"Fact {fid} not found")
        if "goal" in body.from_:
            raise HTTPException(400, "goal cannot be used in from")
        intent = edge_store.create_intent(conn, project_id, body.from_, body.description, body.worker, worker=body.worker)
        # Point the intent's to_fact_id to 'goal' to mark completion.
        conn.execute(
            "UPDATE intents SET to_fact_id = 'goal', concluded_at = ? WHERE id = ? AND project_id = ?",
            (intent.created_at, intent.id, project_id),
        )
        if body.flag:
            graph_store.set_flag(conn, project_id, body.flag)
        graph_store.set_status(conn, project_id, "flag_found")
        graph_store.add_link(conn, project_id, f"fact:{body.from_[0]}", "flag", "flag")
        intent_model = edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent.id), project_id)
    state.logger.project("flag_found", project_id, worker=body.worker, flag=body.flag)
    if state.orchestrator is not None:
        state.orchestrator.on_flag_found(project_id)
    return intent_model


@router.get("/broadcasts", response_model=list[Broadcast])
def list_broadcasts(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        return graph_store.list_broadcasts(conn)
