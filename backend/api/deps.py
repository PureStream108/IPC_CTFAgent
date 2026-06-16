from __future__ import annotations

from fastapi import Request

from backend.core.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.ipc
