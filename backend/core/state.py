from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path

from backend.blackboard import graph_store
from backend.core.config import AppConfig, load_config, save_config
from backend.core.logging_util import IPCLogger
from backend.blackboard.db import Database
from backend.mcp.mcp_client import MCPRegistry
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.platform.ret2shell import Ret2ShellClient
from backend.platform.ret2shell_mcp import build_ret2shell_mcp
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
        self.config_dir = config_dir
        if _env_flag("IPC_CLEAN_START"):
            self._clean_runtime_state()
        self.config: AppConfig = load_config(config_dir)
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

        # Operational state lives in RAM: it is wiped when the container is
        # removed. Only what the UI exports (below) is written to disk, so the
        # paths here just name the in-RAM databases.
        data_dir = self.root / "data"
        self.db = Database(data_dir / "graph.db", in_memory=True).configure()
        with self.db.connect() as conn:
            graph_store.reset_project_counter_if_empty(conn)
            self._reserve_existing_project_ids(conn)
        self.memory = MemoryStore(
            data_dir / "memory.db", export_dir=None, in_memory=True
        ).configure()
        self.registry = ToolRegistry(cache_db=data_dir / "tool_cache.db", in_memory=True).load()
        self.catalog = ToolCatalog.load(self.registry)
        self.log_export_dir = Path(
            os.environ.get("IPC_LOG_EXPORT_DIR", self.root / "exports" / "logs")
        )
        self.wp_export_dir = Path(
            os.environ.get("IPC_WP_EXPORT_DIR", self.root / "exports" / "Wp")
        )
        self.memory_export_dir = Path(
            os.environ.get("IPC_MEMORY_EXPORT_DIR", self.root / "exports" / "memory")
        )
        # Derive endpoints allocate collision-free filenames from persistent
        # directories. Serialise allocation + creation within this process.
        self.export_lock = threading.RLock()
        self.logger = IPCLogger(
            self.root / "logs",
            enabled=self.config.log_enabled,
            project_filename_resolver=self.project_log_filename,
        )

        self.limiter = TaskSlotLimiter(
            max_concurrent_tasks=self.config.limits.max_concurrent_tasks,
            total_cpu=self.config.limits.total_cpu,
        )
        self.pool = ContainerPool(
            backend=self.config.runtime.sandbox_backend,
            workspace_root=self.root / "projects",
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
        # The ret2shell competition MCP (dynamic instance control) is only
        # registered when participant credentials are configured, so members
        # never see a platform they cannot reach.
        if os.getenv("IPC_R2S_USERNAME") or os.getenv("IPC_R2S_TOKEN"):
            self.mcps.register(build_ret2shell_mcp(Ret2ShellClient()))

        self.wp_dir = self.root / "wp"
        self.wp_dir.mkdir(parents=True, exist_ok=True)

        # Attached by module 8.
        self.orchestrator = None

    def _reserve_existing_project_ids(self, conn) -> None:
        """Keep an in-memory graph from reusing persistent workspaces.

        Project graph state is deliberately ephemeral, while project sandbox
        directories survive a server restart.  Reserve the largest surviving
        numeric ID so a newly created project never inherits old progress or
        artifacts merely because its in-memory database was recreated.
        """
        highest = 0
        for entry in self.projects_dir.iterdir():
            if not entry.is_dir():
                continue
            match = re.fullmatch(r"proj_(\d+)", entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
        if highest:
            conn.execute(
                "UPDATE counters SET value = MAX(value, ?) WHERE name = 'project'",
                (highest,),
            )

    def _clean_runtime_state(self) -> None:
        # "data" is absent: it holds exports now, and the databases are in RAM.
        for name in ("projects", "memory", "logs", "wp"):
            target = self.root / name
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
        # Called from logging paths, including the scheduler's crash handler;
        # a locked database must degrade to the plain id instead of raising
        # through the logger and killing the caller.
        try:
            with self.db.connect() as conn:
                return graph_store.project_log_filename(conn, project_id)
        except Exception:
            return project_id

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
