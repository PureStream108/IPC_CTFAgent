from __future__ import annotations

import os
import shutil
from pathlib import Path

from backend.blackboard import graph_store
from backend.core.config import AppConfig, load_config, save_config
from backend.core.logging_util import IPCLogger
from backend.blackboard.db import Database
from backend.mcp.antsword import build_antsword_mcp
from backend.mcp.base import MCPRegistry
from backend.mcp.shared import build_browser_mcp, build_ghidra_mcp, build_zap_mcp
from backend.memory.memory_mcp import build_memory_mcp
from backend.memory.memory_store import MemoryStore
from backend.sandbox.container_pool import ContainerPool
from backend.sandbox.network_manager import NetworkManager
from backend.sandbox.resource_limiter import ResourceLimiter
from backend.tools.tool_mcp import build_tool_search_mcp
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

        data_dir = self.root / "data"
        self.db = Database(data_dir / "graph.db").configure()
        with self.db.connect() as conn:
            graph_store.reset_project_counter_if_empty(conn)
        self.memory = MemoryStore(
            data_dir / "memory.db", export_dir=self.root / "memory"
        ).configure()
        self.registry = ToolRegistry(cache_db=data_dir / "tool_cache.db").load()
        self.log_export_dir = Path(
            os.environ.get("IPC_LOG_EXPORT_DIR", self.root / "exports" / "logs")
        )
        self.wp_export_dir = Path(
            os.environ.get("IPC_WP_EXPORT_DIR", self.root / "exports" / "Wp")
        )
        self.logger = IPCLogger(
            self.root / "logs",
            enabled=self.config.log_enabled,
            project_filename_resolver=self.project_log_filename,
        )

        self.limiter = ResourceLimiter(
            total_cpu=self.config.limits.total_cpu,
            total_memory_gb=self.config.limits.total_memory_gb,
            total_disk_gb=self.config.limits.total_disk_gb,
            per_agent_memory_gb=self.config.limits.per_agent_memory_gb,
        )
        self.pool = ContainerPool(
            backend=self.config.runtime.sandbox_backend,
            workspace_root=self.root / "projects",
            limiter=self.limiter,
            network=self.config.limits.network,
        )
        self.network = NetworkManager(backend=self.config.runtime.sandbox_backend)

        # Shared MCP servers (memory + tool_search + browser/ghidra/zap + antsword).
        self.mcps = MCPRegistry()
        self.mcps.register(build_memory_mcp(self.memory))
        self.mcps.register(build_tool_search_mcp(self.registry))
        self.mcps.register(build_browser_mcp())
        self.mcps.register(build_ghidra_mcp())
        self.mcps.register(build_zap_mcp())
        self.mcps.register(build_antsword_mcp())

        self.projects_dir = self.root / "projects"
        self.wp_dir = self.root / "wp"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.wp_dir.mkdir(parents=True, exist_ok=True)

        # Attached by module 8.
        self.orchestrator = None

    def _clean_runtime_state(self) -> None:
        for name in ("data", "projects", "memory", "logs", "wp"):
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

    def save_config(self) -> None:
        save_config(self.config, self.config_dir)
        self.logger.set_enabled(self.config.log_enabled)

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
