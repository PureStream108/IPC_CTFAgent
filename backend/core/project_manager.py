from __future__ import annotations

from pathlib import Path

from backend.sandbox.network_manager import NetworkManager


class ProjectManager:
    def __init__(self, projects_dir: Path, network: NetworkManager):
        self.projects_dir = Path(projects_dir)
        self.network = network

    def ensure_dirs(self, project_id: str) -> Path:
        base = self.projects_dir / project_id
        for sub in ("attachments", "artifacts", "exploit", "evidence", "wp", "graph_snapshot"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        return base

    def start_challenge_env(self, project_id: str):
        att = self.projects_dir / project_id / "attachments"
        return self.network.start(project_id, att)

    def teardown(self, project_id: str) -> None:
        self.network.stop(project_id)
