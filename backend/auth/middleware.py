from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from backend.auth.manager import AuthManager

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
    """Compatibility middleware with authentication disabled.

    IPC is intended to run inside the operator's trusted Docker network.  The
    browser UI therefore opens directly without creating an administrator
    password or maintaining a login session.  The separate runner-token check
    for the internal MCP mount remains in place.
    """

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
        # No administrator setup/login is required for normal browser/API
        # requests.  Keep the state marker for handlers that inspect it.
        request.state.authenticated = True
        return await call_next(request)

    @staticmethod
    def _is_public(request: Request) -> bool:
        path = request.url.path
        method = request.method.upper()
        if path == "/static" or path.startswith("/static/"):
            return method in {"GET", "HEAD"}
        return method in _PUBLIC_METHODS.get(path, frozenset())
