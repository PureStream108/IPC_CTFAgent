from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.api.deps import get_state
from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.models import (
    CompleteRequest,
    ConcludeRequest,
    ConcludeResponse,
    CreateHintRequest,
    CreateIntentRequest,
    CreateProjectRequest,
    Fact,
    HeartbeatRequest,
    Hint,
    Intent,
    ProjectDetail,
    ProjectSummary,
    ReportRequest,
)
from backend.core.config import CATEGORIES
from backend.core.state import AppState

router = APIRouter(tags=["projects"])


def _expire(state: AppState, conn, project_id: str | None = None) -> None:
    it, rt = graph_store.get_timeouts(conn)
    edge_store.expire_workers(conn, it, project_id)
    graph_store.expire_reason_leases(conn, rt, project_id)


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        _expire(state, conn)
        return graph_store.project_summaries(conn)


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest, state: AppState = Depends(get_state)):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    hints = [(h.content, h.creator) for h in (body.hints or [])]
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, body.title, body.origin, body.goal, body.category, hints)
    state.logger.project("project_created", pid, title=body.title, category=body.category)
    with state.db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    return detail


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        _expire(state, conn, project_id)
        detail = graph_store.project_detail(conn, project_id)
    if detail is None:
        raise HTTPException(404, "Project not found")
    return detail


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, state: AppState = Depends(get_state)):
    # tear down this project's sandboxes + challenge env before removing files.
    state.pool.stop_project(project_id)
    state.network.stop(project_id)
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
    state.delete_project_files(project_id)
    with state.db.connect() as conn:
        graph_store.delete_project(conn, project_id)


@router.post("/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: str, file: UploadFile = File(...), state: AppState = Depends(get_state)
):
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
    dest = state.attachments_dir(project_id) / file.filename
    data = await file.read()
    dest.write_bytes(data)
    with state.db.connect() as conn:
        att = graph_store.create_attachment(conn, project_id, file.filename, str(dest))
    state.logger.project("attachment_uploaded", project_id, filename=file.filename, size=len(data))
    return att


# ---- hints ----


@router.post("/projects/{project_id}/hints", response_model=Hint, status_code=201)
def create_hint(project_id: str, body: CreateHintRequest, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
        return graph_store.create_hint(conn, project_id, body.content, body.creator)


# ---- intents (knowledge-graph protocol) ----


def _require_active(conn, project_id: str):
    row = graph_store.get_project_row(conn, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    if row["status"] in ("completed", "stopped"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


@router.post("/projects/{project_id}/intents", response_model=Intent, status_code=201)
def create_intent(project_id: str, body: CreateIntentRequest, state: AppState = Depends(get_state)):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        for fid in body.from_:
            if not node_store.fact_exists(conn, project_id, fid):
                raise HTTPException(404, f"Fact {fid} not found")
        if "goal" in body.from_:
            raise HTTPException(400, "goal cannot be used in from")
        if body.worker is not None and body.worker != body.creator:
            raise HTTPException(400, "worker must be null or equal to creator")
        return edge_store.create_intent(conn, project_id, body.from_, body.description, body.creator, body.worker)


@router.post("/projects/{project_id}/intents/{intent_id}/heartbeat", response_model=Intent)
def heartbeat_intent(
    project_id: str, intent_id: str, body: HeartbeatRequest, state: AppState = Depends(get_state)
):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        _expire(state, conn, project_id)
        row = edge_store.get_intent(conn, project_id, intent_id)
        if row is None:
            raise HTTPException(404, "Intent not found")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
        edge_store.claim_intent(conn, project_id, intent_id, body.worker)
        return edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)


@router.post("/projects/{project_id}/intents/{intent_id}/release", response_model=Intent)
def release_intent(
    project_id: str, intent_id: str, body: HeartbeatRequest, state: AppState = Depends(get_state)
):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        row = edge_store.get_intent(conn, project_id, intent_id)
        if row is None:
            raise HTTPException(404, "Intent not found")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
        if row["worker"] == body.worker:
            edge_store.release_intent(conn, project_id, intent_id)
        return edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)


@router.post("/projects/{project_id}/intents/{intent_id}/conclude", response_model=ConcludeResponse)
def conclude_intent(
    project_id: str, intent_id: str, body: ConcludeRequest, state: AppState = Depends(get_state)
):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        _expire(state, conn, project_id)
        row = edge_store.get_intent(conn, project_id, intent_id)
        if row is None:
            raise HTTPException(404, "Intent not found")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Intent already concluded")
        if row["worker"] is not None and row["worker"] != body.worker:
            raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
        fact = node_store.create_fact(conn, project_id, body.description)
        edge_store.conclude_intent(conn, project_id, intent_id, body.worker, fact.id)
        graph_store.touch_project(conn, project_id)
        intent = edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)
    state.logger.project("intent_concluded", project_id, intent=intent_id, worker=body.worker, fact=fact.id)
    return ConcludeResponse(fact=fact, intent=intent)
