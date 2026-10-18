from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import psycopg.errors
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from backend.api.deps import get_state
from backend.blackboard import edge_store, graph_store, node_store
from backend.blackboard.models import (
    ConcludeRequest,
    ConcludeResponse,
    CreateHintRequest,
    CreateIntentRequest,
    CreateProjectRequest,
    HeartbeatRequest,
    Hint,
    Intent,
    ProjectDetail,
    ProjectSummary,
    ReasonClaimRequest,
)
from backend.core.config import CATEGORIES
from backend.core.redaction import redact_object
from backend.core.state import AppState

router = APIRouter(tags=["projects"])
MAX_PROJECT_ATTACHMENT_BYTES = 100 * 1024 * 1024

_T = TypeVar("_T")

_last_project_detail: dict[str, ProjectDetail] = {}
_last_project_list: dict[str, list[ProjectSummary]] = {}


def _with_deadlock_retry(operation: Callable[[], _T]) -> _T:
    """Run a DB operation, retrying once on a deadlock.

    A migration ``ALTER TABLE`` and a read-path lease-expiry ``UPDATE`` can
    deadlock; Postgres kills one with DeadlockDetected. The victim's locks are
    already gone by the time this returns, so an immediate single retry almost
    always succeeds. The retry is deliberately bounded to one: a persistent
    deadlock is a real problem the caller must still see.
    """
    try:
        return operation()
    except psycopg.errors.DeadlockDetected:
        return operation()


def _expire(state: AppState, conn, project_id: str | None = None) -> None:
    it, rt = graph_store.get_timeouts(conn)
    edge_store.expire_workers(conn, it, project_id)
    graph_store.expire_reason_leases(conn, rt, project_id)


def _public_detail(detail):
    if detail is None:
        return None
    secret = detail.project.flag
    return detail.__class__.model_validate(redact_object(detail.model_dump(mode="json"), secret))



_GET_STATE_DEPENDENCY = Depends(get_state)

@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(state: AppState = _GET_STATE_DEPENDENCY, category: str | None = None):
    if category is not None and category not in CATEGORIES:
        raise HTTPException(400, "unknown project category")
    def read() -> list[ProjectSummary]:
        with state.db.connect() as conn:
            _expire(state, conn)
            return graph_store.project_summaries(conn)

    try:
        summaries = _with_deadlock_retry(read)
    except psycopg.errors.OperationalError:
        if "all" in _last_project_list:
            return [p for p in _last_project_list["all"] if category is None or p.category == category]
        raise
    _last_project_list["all"] = summaries
    return [p for p in summaries if category is None or p.category == category]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest, state: AppState = _GET_STATE_DEPENDENCY):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    if body.pentest is not None:
        from backend.pentest.models import Setup
        try:
            Setup.model_validate(body.pentest)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.category != "pentest":
            raise HTTPException(400, "pentest setup requires pentest category")
    hints = [(h.content, h.creator) for h in (body.hints or [])]
    with state.db.connect() as conn:
        pid = graph_store.create_project(conn, body.title, body.origin, body.goal, body.category, hints, pentest_setup=body.pentest)
    state.logger.project("project_created", pid, title=body.title, category=body.category)
    with state.db.connect() as conn:
        return _public_detail(graph_store.project_detail(conn, pid))


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, state: AppState = _GET_STATE_DEPENDENCY):
    def read():
        with state.db.connect() as conn:
            _expire(state, conn, project_id)
            return graph_store.project_detail(conn, project_id)

    try:
        detail = _with_deadlock_retry(read)
    except psycopg.errors.OperationalError:
        cached = _last_project_detail.get(project_id)
        if cached is not None:
            return cached
        raise
    if detail is None:
        raise HTTPException(404, "Project not found")
    public = _public_detail(detail)
    _last_project_detail[project_id] = public
    return public


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, state: AppState = _GET_STATE_DEPENDENCY):
    state.pool.stop_project(project_id)
    state.network.stop(project_id)
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
    state.delete_project_files(project_id)
    with state.db.connect() as conn:
        graph_store.delete_project(conn, project_id)



_REQUIRED_UPLOAD_FILE = File(...)

@router.post("/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: str, file: UploadFile = _REQUIRED_UPLOAD_FILE, state: AppState = _GET_STATE_DEPENDENCY
):
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
    filename = Path(file.filename or "").name
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "attachment filename is required")
    if file.size is not None and file.size > MAX_PROJECT_ATTACHMENT_BYTES:
        raise HTTPException(413, "attachment exceeds the size limit")
    destination = state.attachments_dir(project_id)
    destination.mkdir(parents=True, exist_ok=True)
    dest = destination / filename
    written = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_PROJECT_ATTACHMENT_BYTES:
                    raise HTTPException(413, "attachment exceeds the size limit")
                handle.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    with state.db.connect() as conn:
        att = graph_store.create_attachment(conn, project_id, filename, str(dest))
    state.logger.project("attachment_uploaded", project_id, filename=filename, size=written)
    return att




@router.post("/projects/{project_id}/hints", response_model=Hint, status_code=201)
def create_hint(project_id: str, body: CreateHintRequest, state: AppState = _GET_STATE_DEPENDENCY):
    with state.db.connect() as conn:
        if graph_store.get_project_row(conn, project_id) is None:
            raise HTTPException(404, "Project not found")
        return graph_store.create_hint(conn, project_id, body.content, body.creator)




def _require_active(conn, project_id: str):
    row = graph_store.get_project_row(conn, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    if row["status"] in ("solved", "stopped", "timeout", "infra_error", "failed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


@router.post("/projects/{project_id}/intents", response_model=Intent, status_code=201)
def create_intent(project_id: str, body: CreateIntentRequest, state: AppState = _GET_STATE_DEPENDENCY):
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
    project_id: str, intent_id: str, body: HeartbeatRequest, state: AppState = _GET_STATE_DEPENDENCY
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
        token = edge_store.claim_intent(
            conn,
            project_id,
            intent_id,
            body.worker,
            lease_owner=body.worker,
            lease_token=body.lease_token,
        )
        if token is None:
            raise HTTPException(409, "Intent lease is owned by another worker or token is stale")
        return edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)


@router.post("/projects/{project_id}/intents/{intent_id}/release", response_model=Intent)
def release_intent(
    project_id: str, intent_id: str, body: HeartbeatRequest, state: AppState = _GET_STATE_DEPENDENCY
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
            released = edge_store.release_intent(
                conn,
                project_id,
                intent_id,
                lease_owner=body.worker if body.lease_token else None,
                lease_token=body.lease_token,
                worker=body.worker,
            )
            if not released:
                raise HTTPException(409, "Intent lease token is stale")
        return edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)




@router.post("/projects/{project_id}/reason/claim")
def claim_reason(project_id: str, body: ReasonClaimRequest, state: AppState = _GET_STATE_DEPENDENCY):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        _, reason_timeout = graph_store.get_timeouts(conn)
        graph_store.expire_reason_leases(conn, reason_timeout, project_id)
        current = graph_store.reason_holder(conn, project_id)
        if current is not None and current.worker != body.worker:
            raise HTTPException(409, f"Reason is currently claimed by {current.worker}")
        return graph_store.claim_reason(conn, project_id, body.worker, body.trigger)


@router.post("/projects/{project_id}/reason/heartbeat")
def heartbeat_reason(project_id: str, body: HeartbeatRequest, state: AppState = _GET_STATE_DEPENDENCY):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        _, reason_timeout = graph_store.get_timeouts(conn)
        graph_store.expire_reason_leases(conn, reason_timeout, project_id)
        current = graph_store.reason_holder(conn, project_id)
        if current is not None and current.worker != body.worker:
            raise HTTPException(409, f"Reason is currently claimed by {current.worker}")
        renewed = graph_store.heartbeat_reason(conn, project_id, body.worker)
        if renewed is None:
            raise HTTPException(404, "Reason claim not found")
        return renewed


@router.post("/projects/{project_id}/reason/release")
def release_reason(project_id: str, body: HeartbeatRequest, state: AppState = _GET_STATE_DEPENDENCY):
    with state.db.connect() as conn:
        _require_active(conn, project_id)
        current = graph_store.reason_holder(conn, project_id)
        if current is not None and current.worker != body.worker:
            raise HTTPException(409, f"Reason is currently claimed by {current.worker}")
        graph_store.clear_reason(conn, project_id)
    return {"released": True}


@router.post("/projects/{project_id}/intents/{intent_id}/conclude", response_model=ConcludeResponse)
def conclude_intent(
    project_id: str, intent_id: str, body: ConcludeRequest, state: AppState = _GET_STATE_DEPENDENCY
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
        if body.lease_token:
            concluded = edge_store.conclude_intent(
                conn,
                project_id,
                intent_id,
                body.worker,
                fact.id,
                lease_owner=body.worker,
                lease_token=body.lease_token,
            )
        else:
            concluded = edge_store.conclude_intent(
                conn, project_id, intent_id, body.worker, fact.id
            )
        if not concluded:
            raise HTTPException(409, "Intent lease token is stale")
        graph_store.touch_project(conn, project_id)
        intent = edge_store.intent_to_model(conn, edge_store.get_intent(conn, project_id, intent_id), project_id)
    state.logger.project("intent_concluded", project_id, intent=intent_id, worker=body.worker, fact=fact.id)
    return ConcludeResponse(fact=fact, intent=intent)
