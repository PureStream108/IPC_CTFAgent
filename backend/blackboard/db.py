from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

PROJECT_STATES = (
    "created",
    "running",
    "flag_found",
    "wp_writing",
    "memory_writing",
    "completed",
    "stopped",
)

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 30,
    reason_timeout INTEGER NOT NULL DEFAULT 30
);
INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 30, 30);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'misc',
    status TEXT NOT NULL DEFAULT 'created',
    flag TEXT,
    wp_path TEXT,
    log_filename TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

-- Orchestration overlay: which agents are in a project and their state.
CREATE TABLE IF NOT EXISTS agents (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,          -- 'ipc' | 'diamond' | member name
    role TEXT NOT NULL,          -- 'ipc' | 'diamond' | 'member'
    state TEXT NOT NULL DEFAULT 'idle',  -- idle|active|paused|done
    start_fact_id TEXT,          -- node Diamond assigned as the start point
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, name)
);

-- Orchestration lines drawn in the graph (Seed.md 流程图).
CREATE TABLE IF NOT EXISTS agent_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    src TEXT NOT NULL,           -- agent name or node ref ('fact:f001','flag','wp')
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,          -- assign|report|explore|flag|wp|return|start
    created_at TEXT NOT NULL
);

-- Member difficulty reports to Diamond.
CREATE TABLE IF NOT EXISTS reports (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    member TEXT NOT NULL,
    node_id TEXT,                -- submission node (fact id) the report attaches to
    progress TEXT NOT NULL,
    difficulty TEXT NOT NULL,    -- low|medium|high|ex (free text tolerated)
    steps_json TEXT NOT NULL,    -- existing problem-solving steps
    directions_json TEXT NOT NULL,  -- directions for other Members
    knowledge_json TEXT NOT NULL,   -- involved knowledge points
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

-- Files attached at project creation.
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    title TEXT NOT NULL,
    flag TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


class Database:

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._configured = False

    def configure(self) -> "Database":
        with self._lock:
            if self._configured:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.executescript(SCHEMA)
            self._configured = True
        return self

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
