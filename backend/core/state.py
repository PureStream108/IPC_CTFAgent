from __future__ import annotations

import os
import shutil
import socket
import threading
import uuid
from pathlib import Path

from backend.blackboard import graph_store
from backend.core.config import AppConfig, load_config, save_config
from backend.core.logging_util import IPCLogger
from backend.blackboard.db import Database
from backend.mcp.mcp_client import MCPRegistry
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.network_manager import NetworkManager
from backend.sandbox.resource_limiter import TaskSlotLimiter
from backend.tools.tool_mcp import build_tool_search_mcp
from backend.tools.catalog import ToolCatalog
from backend.tools.tool_registry import ToolRegistry


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class AppState:
    def __init__(self, root: str | Path = ".", config_dir: Path | None = None):
        self.root = Path(root)
        configured_artifact_root = os.environ.get("IPC_ARTIFACT_ROOT", "").strip()
        self.artifact_root = (
            Path(configured_artifact_root).expanduser()
            if configured_artifact_root
            else self.root / "data" / "artifacts"
        )
        self.projects_dir = self.artifact_root / "projects"
        self.wp_dir = self.artifact_root / "writeups"
        self.logs_dir = self.artifact_root / "logs"
        self.export_dir = self.artifact_root / "exports"
        self.log_export_dir = self.export_dir / "logs"
        self.wp_export_dir = self.export_dir / "writeups"
        self.memory_export_dir = self.export_dir / "memory"
        self.instance_id = os.environ.get("IPC_INSTANCE_ID") or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self.config_dir = config_dir
        if _env_flag("IPC_CLEAN_START"):
            self._clean_runtime_state()
        self.config: AppConfig = load_config(config_dir)

        # PostgreSQL is the only runtime fact store. Large and generated files
        # live below one deployment-shared Artifact root so every app instance
        # sees the same workspaces, attachments, logs, writeups and exports.
        self.db = Database().configure()
        with self.db.connect() as conn:
            graph_store.reset_project_counter_if_empty(conn)
        self.memory = MemoryStore(self.db, export_dir=None).configure()
        self.registry = ToolRegistry().load()
        self.catalog = ToolCatalog.load(self.registry)
        for directory in (
            self.projects_dir,
            self.wp_dir,
            self.logs_dir,
            self.log_export_dir,
            self.wp_export_dir,
            self.memory_export_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        # Derive endpoints allocate collision-free filenames from persistent
        # directories. Serialise allocation + creation within this process.
        self.export_lock = threading.RLock()
        self.logger = IPCLogger(
            self.logs_dir,
            enabled=self.config.log_enabled,
            project_filename_resolver=self.project_log_filename,
        )

        self.limiter = TaskSlotLimiter(
            max_concurrent_tasks=self.config.limits.max_concurrent_tasks,
            total_cpu=self.config.limits.total_cpu,
        )
        self.pool = ContainerPool(
            backend=self.config.runtime.sandbox_backend,
            workspace_root=self.projects_dir,
            limiter=self.limiter,
            network=self.config.limits.network,
        )
        self.network = NetworkManager(backend=self.config.runtime.sandbox_backend)

        # In-process MCP servers (memory + tool_search). The container-backed
        # servers (browser, reverse, zap) run inside each task container and are
        # injected per Member by the orchestrator via docker-exec stdio targets.
        self.mcps = MCPRegistry()
        self.mcps.register(build_memory_mcp(self.memory, catalog=self.catalog))
        self.mcps.register(build_tool_search_mcp(self.registry))

        # Attached by module 8.
        self.orchestrator = None

    def _clean_runtime_state(self) -> None:
        # Database rows are intentionally not deleted by a local clean start.
        # Durable exports are preserved; only live workspaces and generated
        # output are cleared from the shared Artifact root.
        for target in (self.projects_dir, self.wp_dir, self.logs_dir):
            if not target.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    def reload_config(self) -> None:
        self.config = load_config(self.config_dir)
        self.logger.set_enabled(self.config.log_enabled)
        if self.orchestrator is not None:
            self.orchestrator.reload_config()

    def save_config(self) -> None:
        save_config(self.config, self.config_dir)
        self.logger.set_enabled(self.config.log_enabled)
        if self.orchestrator is not None:
            self.orchestrator.reload_config()

    def close(self) -> None:
        """Release resources owned by the application state."""
        try:
            self.registry.close()
        finally:
            try:
                self.memory.close()
            finally:
                self.db.close()

    def project_log_filename(self, project_id: str) -> str | None:
        with self.db.connect() as conn:
            return graph_store.project_log_filename(conn, project_id)

    def attachments_dir(self, project_id: str) -> Path:
        d = self.projects_dir / project_id / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def delete_project_files(self, project_id: str) -> None:
        target = (self.projects_dir / project_id).resolve()
        root = self.projects_dir.resolve()
        if target == root or root not in target.parents:
            raise ValueError(f"refusing to delete project path outside projects dir: {target}")
        shutil.rmtree(target, ignore_errors=True)
        self.logger.delete_project_logs(project_id)
