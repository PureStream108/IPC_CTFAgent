from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.api.deps import get_state
from backend.competition.service import CompetitionService
from backend.competition.store import CompetitionConflict
from backend.core.state import AppState
from backend.core.validation import InputValidationError

router = APIRouter(prefix="/api", tags=["competition"])


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)
    select: list[str] | None = Field(default=None, max_length=1000)

    @field_validator("select")
    @classmethod
    def validate_select(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise InputValidationError("challenge ids must be strings")
            item = item.strip()
            if not item or len(item) > 256:
                raise ValueError("challenge ids must be 1 to 256 characters")
            if item not in cleaned:
                cleaned.append(item)
        if not cleaned:
            raise ValueError("select must contain at least one challenge id")
        return cleaned


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    operation_id: str | None = Field(default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    limits: dict[str, int] = Field(default_factory=dict)
    max_concurrent_tasks: int | None = None
    total_cpu: int | None = None
    remote_concurrency: int | None = None


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    flag: str = Field(min_length=1, max_length=4096)
    evidence: str = Field(min_length=1, max_length=12000)
    instance_generation: int = Field(ge=0)



_GET_STATE_DEPENDENCY = Depends(get_state)

def get_competition(state: AppState = _GET_STATE_DEPENDENCY) -> CompetitionService:
    service = getattr(state, "competition", None)
    if service is None:
        service = CompetitionService(state)
        state.competition = service
        service.start_worker()
    return service


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "competition resource not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except CompetitionConflict as exc:
        headers = {}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        raise HTTPException(409, str(exc), headers=headers) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc



_GET_COMPETITION_DEPENDENCY = Depends(get_competition)

@router.post("/workflows/{workflow_id}/preflight")
def preflight(
    workflow_id: str,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return _call(service.preflight, workflow_id)


@router.post("/workflows/{workflow_id}/start", status_code=201)
def start(
    workflow_id: str,
    body: StartRequest,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return _call(service.start, workflow_id, body.idempotency_key, body.select)


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return {"runs": service.store.list_runs(limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, service: CompetitionService = _GET_COMPETITION_DEPENDENCY):
    return _call(service.store.run_snapshot, run_id)


@router.get("/runs/{run_id}/members")
def get_members(run_id: str, service: CompetitionService = _GET_COMPETITION_DEPENDENCY):
    return {"members": _call(service.store.member_snapshot, run_id)}


@router.post("/runs/{run_id}/{action}")
def control_run(
    run_id: str,
    action: Literal["pause", "resume", "stop", "refresh"],
    body: ControlRequest,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    if body.operation_id:
        return _call(service.control, run_id, action, body.revision, operation_id=body.operation_id)
    return _call(service.control, run_id, action, body.revision)


@router.get("/runs/{run_id}/operations/{operation_id}")
def control_operation(run_id: str, operation_id: str, service: CompetitionService = _GET_COMPETITION_DEPENDENCY):
    return _call(service.store.control_operation, run_id, operation_id)


@router.patch("/runs/{run_id}/limits")
def patch_run_limits(
    run_id: str,
    body: LimitsRequest,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    limits = {**body.limits}
    for key in ("max_concurrent_tasks", "total_cpu", "remote_concurrency"):
        value = getattr(body, key)
        if value is not None:
            limits[key] = value
    return _call(service.update_limits, run_id, limits, body.revision)


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc
    _call(service.store.get_run, run_id)

    def stream():
        current = cursor
        idle_since = time.monotonic()
        while not service._stop.is_set():
            events = service.store.run_events(run_id, current, 200)
            if events:
                for event in events:
                    current = event["event_id"]
                    yield (
                        f"id: {current}\n"
                        f"event: {event['kind']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 15:
                yield ": keepalive\n\n"
                idle_since = time.monotonic()
            time.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/observations")
def run_observations(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return {"observations": _call(service.store.observations, run_id, limit=limit)}


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    _call(service.store.session, session_id)
    return {"events": service.store.events(session_id, after, limit)}


@router.post("/challenges/{challenge_id}/candidates", status_code=202)
def submit_candidate(
    challenge_id: str,
    body: CandidateRequest,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return _call(
        service.submit_candidate,
        body.run_id,
        challenge_id,
        body.session_id,
        body.flag,
        body.evidence,
        body.instance_generation,
    )


@router.get("/submissions/{submission_id}")
def get_submission(
    submission_id: str,
    service: CompetitionService = _GET_COMPETITION_DEPENDENCY,
):
    return _call(service.store.submission, submission_id)


@router.post("/wp-jobs/{job_id}/retry")
def retry_wp(job_id: str, service: CompetitionService = _GET_COMPETITION_DEPENDENCY):
    return _call(service.retry_wp, job_id)
