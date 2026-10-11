"""Agent session observability: tell "alive" apart from "making progress".

The UI used to render four different facts as one green dot -- the API is up,
the coordinator is ticking, a worker holds the seat, and the task is actually
advancing. A run could look perfectly healthy while a Member had been silent
for twenty minutes.

These endpoints expose each fact separately, straight from
``agent_session_health``, plus a merged event stream so a Member card can show
what its session is doing right now rather than only that it exists.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.agent.jobs import AgentJobStore
from backend.agent.session import AgentSessionStore
from backend.agent.watchdog import classify
from backend.api.deps import get_state
from backend.core.state import AppState


router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_sessions(state: AppState = Depends(get_state)) -> AgentSessionStore:
    return AgentSessionStore(state.db)


def get_jobs(state: AppState = Depends(get_state)) -> AgentJobStore:
    return AgentJobStore(state.db)


def _view(row: dict) -> dict:
    """Attach the watchdog's own grading to a health row.

    The API returns the same verdict the coordinator acts on, so the console
    cannot disagree with the system about whether a session is stalled.
    """
    verdict = classify(row)
    return {
        **row,
        "health": verdict.level,
        "health_reason": verdict.reason,
    }


@router.get("/sessions")
def list_sessions(
    kind: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    state_filter: str | None = Query(default="active", alias="state"),
    limit: int = Query(default=100, ge=1, le=500),
    sessions: AgentSessionStore = Depends(get_sessions),
):
    states: tuple[str, ...] = ()
    if state_filter and state_filter != "all":
        states = tuple(
            item.strip() for item in state_filter.split(",") if item.strip()
        )
    rows = sessions.health_for_scope(
        kind=kind, task_key=task_key, states=states, limit=limit
    )
    return {"sessions": [_view(row) for row in rows]}


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    sessions: AgentSessionStore = Depends(get_sessions),
    jobs: AgentJobStore = Depends(get_jobs),
):
    try:
        session = sessions.session(session_id)
    except KeyError as exc:
        raise HTTPException(404, "agent session not found") from exc
    health = sessions.health(session_id)
    return {
        "session": session,
        "health": _view(health) if health else None,
        "turns": sessions.turns(session_id, limit=20),
        "jobs": jobs.jobs(session_id, limit=20),
    }


@router.get("/sessions/{session_id}/jobs")
def session_jobs(
    session_id: str,
    active_only: bool = Query(default=False),
    jobs: AgentJobStore = Depends(get_jobs),
):
    return {"jobs": jobs.jobs(session_id, active_only=active_only, limit=100)}


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    sessions: AgentSessionStore = Depends(get_sessions),
    state: AppState = Depends(get_state),
):
    """Stream one session's transcript, resumable by ``event_id``.

    Same cursor contract as the run event stream: ``Last-Event-ID`` wins over
    the query parameter, so a reconnecting browser resumes exactly where it
    stopped rather than replaying or skipping events.
    """
    try:
        sessions.session(session_id)
    except KeyError as exc:
        raise HTTPException(404, "agent session not found") from exc
    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc

    def stream():
        current = cursor
        idle_since = time.monotonic()
        while True:
            events = sessions.events_after_event_id(session_id, current, 200)
            if events:
                for event in events:
                    current = event["event_id"]
                    yield (
                        f"id: {current}\n"
                        f"event: {event['kind']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    )
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= 15:
                yield ": keepalive\n\n"
                idle_since = time.monotonic()
            time.sleep(0.5)

    del state
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events")
def scope_events(
    kind: str | None = Query(default=None),
    task_key: str | None = Query(default=None),
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    sessions: AgentSessionStore = Depends(get_sessions),
):
    """Stream every session in a scope through one connection.

    A list view wants the newest activity across all live sessions. Doing that
    per session would be one request each; ``uq_agent_event_id`` makes the
    whole scope a single ordered scan, which is what this uses.
    """
    cursor = after
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID must be an integer") from exc

    def stream():
        current = cursor
        idle_since = time.monotonic()
        while True:
            events = sessions.events_for_scope_after_event_id(
                current, kind=kind, task_key=task_key, limit=200
            )
            if events:
                for event in events:
                    current = event["event_id"]
                    yield (
                        f"id: {current}\n"
                        f"event: {event['kind']}\n"
                        f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
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
