from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backend.auth.manager import SESSION_COOKIE_NAME, AuthManager

_PUBLIC_METHODS: dict[str, frozenset[str]] = {
    "/": frozenset({"GET", "HEAD"}),
    "/health": frozenset({"GET", "HEAD"}),
    "/login": frozenset({"GET", "HEAD"}),
    "/setup": frozenset({"GET", "HEAD"}),
    "/auth/status": frozenset({"GET", "HEAD"}),
    "/auth/setup": frozenset({"POST"}),
    "/auth/login": frozenset({"POST"}),
}
_RUNNER_MCP_PREFIX = "/internal/mcp"


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Require a valid signed session for every non-public HTTP endpoint."""

    def __init__(self, app, *, auth_manager: AuthManager) -> None:
        super().__init__(app)
        self.auth_manager = auth_manager
        self.runner_token = os.environ.get("IPC_RUNNER_TOKEN", "").strip()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path.startswith(_RUNNER_MCP_PREFIX):
            if not self.runner_token:
                return JSONResponse(status_code=404, content={"detail": "not found"})
            supplied = request.headers.get("X-IPC-Runner-Token", "")
            if not hmac.compare_digest(supplied, self.runner_token):
                return JSONResponse(status_code=401, content={"detail": "runner authentication required"})
            request.state.authenticated = True
            return await call_next(request)
        if self._is_public(request):
            response = await call_next(request)
            if request.url.path.startswith("/auth/"):
                response.headers["Cache-Control"] = "no-store"
            return response
        if self.auth_manager.configuration_error:
            return JSONResponse(
                status_code=503,
                content={"detail": "authentication configuration is unavailable"},
                headers={"Cache-Control": "no-store"},
            )
        if self.auth_manager.setup_required:
            return JSONResponse(
                status_code=428,
                content={"detail": "initial setup is required"},
                headers={"Cache-Control": "no-store"},
            )
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not self.auth_manager.verify_session(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "authentication required"},
                headers={
                    "Cache-Control": "no-store",
                    "WWW-Authenticate": "Cookie",
                },
            )
        request.state.authenticated = True
        response = await call_next(request)
        if request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _is_public(request: Request) -> bool:
        path = request.url.path
        method = request.method.upper()
        if path == "/static" or path.startswith("/static/"):
            return method in {"GET", "HEAD"}
        return method in _PUBLIC_METHODS.get(path, frozenset())
