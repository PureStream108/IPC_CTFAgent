from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import config as config_router
from backend.api import flags as flags_router
from backend.api import graph as graph_router
from backend.api import logs as logs_router
from backend.api import memory as memory_router
from backend.api import platform as platform_router
from backend.api import project as project_router
from backend.api import solve as solve_router
from backend.api import wp as wp_router
from backend.core.state import AppState
from backend.sandbox.webui_proxy import webui_proxy_manager


def _resolve_frontend_dir(app_root: Path) -> Path:
    """Locate the top-level ``frontend/`` web UI directory.

    The UI no longer lives inside the ``backend`` package, so resolve it from
    (1) an explicit ``IPC_FRONTEND_DIR`` override, (2) the repo root relative to
    this file, or (3) ``<IPC_ROOT>/frontend`` (the Docker layout at /app).
    """
    override = os.environ.get("IPC_FRONTEND_DIR")
    if override:
        return Path(override)
    repo_candidate = Path(__file__).resolve().parents[2] / "frontend"
    if repo_candidate.exists():
        return repo_candidate
    return app_root / "frontend"


def create_app(root: str | Path | None = None) -> FastAPI:
    app_root = Path(root) if root else Path(os.environ.get("IPC_ROOT", "."))
    frontend_dir = _resolve_frontend_dir(app_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = AppState(root=app_root)
        app.state.ipc = state
        # Attach orchestrator if available (module 8).
        try:
            from backend.core.orchestrator import Orchestrator

            state.orchestrator = Orchestrator(state)
            state.orchestrator.start()
        except Exception as exc:  # orchestrator optional during early bring-up
            app.state.orchestrator_error = str(exc)
        try:
            yield
        finally:
            try:
                if state.orchestrator is not None:
                    state.orchestrator.shutdown()
            finally:
                try:
                    state.pool.stop_all()
                finally:
                    try:
                        webui_proxy_manager.close_all()
                    finally:
                        state.close()

    app = FastAPI(title="IPC_CTFAgent", description="Multi-agent CTF solver", lifespan=lifespan)

    app.include_router(project_router.router)
    app.include_router(solve_router.router)
    app.include_router(graph_router.router)
    app.include_router(memory_router.router)
    app.include_router(config_router.router)
    app.include_router(logs_router.router)
    app.include_router(wp_router.router)
    app.include_router(platform_router.router)
    app.include_router(flags_router.router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(frontend_dir / "index.html")

    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

    return app


app = create_app()
