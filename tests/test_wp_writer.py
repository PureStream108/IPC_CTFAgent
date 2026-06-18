from __future__ import annotations

from pathlib import Path

from backend.blackboard import graph_store
from backend.blackboard.db import Database
from backend.core.config import AppConfig
from backend.core.diamond import Diamond
from backend.core.logging_util import IPCLogger
from backend.core.wp_writer import WP_PROMPT


def test_diamond_writes_wp_prompt_only(tmp_path):
    db = Database(tmp_path / "graph.db").configure()
    with db.connect() as conn:
        pid = graph_store.create_project(conn, "Stack", "http://example.test/", "获得 FLAG", "web")

    diamond = Diamond(db, AppConfig(), IPCLogger(tmp_path / "logs"))
    wp_path = Path(diamond.write_wp(pid, tmp_path / "wp"))

    assert wp_path.read_text(encoding="utf-8") == WP_PROMPT + "\n"
    with db.connect() as conn:
        detail = graph_store.project_detail(conn, pid)
    assert detail.project.wp_path == str(wp_path)
