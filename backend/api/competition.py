from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import get_state
from backend.competition.service import CompetitionService
from backend.competition.store import CompetitionConflict
from backend.core.state import AppState


router = APIRouter(prefix="/api", tags=["competition"])


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=200)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


class CandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    flag: str = Field(min_length=1, max_length=4096)
    evidence: str = Field(min_length=1, max_length=12000)
    instance_generation: int = Field(ge=0)


def get_competition(state: AppState = Depends(get_state)) -> CompetitionService:
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
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/workflows/{workflow_id}/preflight")
def preflight(
    workflow_id: str,
    service: CompetitionService = Depends(get_competition),
):
    return _call(service.preflight, workflow_id)


@router.post("/workflows/{workflow_id}/start", status_code=201)
def start(
    workflow_id: str,
    body: StartRequest,
    service: CompetitionService = Depends(get_competition),
):
    return _call(service.start, workflow_id, body.idempotency_key)


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    service: CompetitionService = Depends(get_competition),
):
    return {"runs": service.store.list_runs(limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, service: CompetitionService = Depends(get_competition)):
    return _call(service.store.run_snapshot, run_id)


@router.get("/runs/{run_id}/members")
def get_members(run_id: str, service: CompetitionService = Depends(get_competition)):
    return {"members": _call(service.store.member_snapshot, run_id)}


@router.post("/runs/{run_id}/{action}")
def control_run(
    run_id: str,
    action: Literal["pause", "resume", "stop", "refresh"],
    body: ControlRequest,
    service: CompetitionService = Depends(get_competition),
):
    return _call(service.control, run_id, action, body.revision)


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    service: CompetitionService = Depends(get_competition),
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
    service: CompetitionService = Depends(get_competition),
):
    return {"observations": _call(service.store.observations, run_id, limit=limit)}


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    service: CompetitionService = Depends(get_competition),
):
    _call(service.store.session, session_id)
    return {"events": service.store.events(session_id, after, limit)}


@router.post("/challenges/{challenge_id}/candidates", status_code=202)
def submit_candidate(
    challenge_id: str,
    body: CandidateRequest,
    service: CompetitionService = Depends(get_competition),
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
    service: CompetitionService = Depends(get_competition),
):
    return _call(service.store.submission, submission_id)


@router.post("/wp-jobs/{job_id}/retry")
def retry_wp(job_id: str, service: CompetitionService = Depends(get_competition)):
    return _call(service.retry_wp, job_id)
