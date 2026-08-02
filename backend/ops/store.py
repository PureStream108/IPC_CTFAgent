from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.config import LLMConfig
from backend.ops.models import PlatformWorkflowSpec, validate_secret_name

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    claude_session_id TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    response_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_session_id ON runs(session_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active
    ON runs(session_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id, id);

CREATE TABLE IF NOT EXISTS session_projects (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, project_id)
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    confirmed_digest TEXT,
    capability_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CLAUDE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]{8,128}$")
_TERMINAL_RUN_STATUSES = {"completed", "interrupted", "error", "abandoned"}


class OpsStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "data" / "ops-agent"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.config_path = self.root / "config.json"
        self.secrets_path = self.root / "secrets.json"
        self.database_path = self.root / "history.db"
        self._lock = threading.RLock()
        self._configure_database()

    def _configure_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "claude_session_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN claude_session_id TEXT")
            event_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "run_id" not in event_columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN run_id TEXT REFERENCES runs(id) ON DELETE SET NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id)"
            )
        _restrict_file(self.database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def load_llm_config(self) -> LLMConfig:
        with self._lock:
            public = self._read_json(self.config_path, {})
            secret_data = self._read_secrets()
        data = {
            "api_format": public.get("api_format", "openai"),
            "api_surface": public.get("api_surface", "auto"),
            "reasoning_effort": public.get("reasoning_effort", "auto"),
            "base_url": public.get("base_url", ""),
            "model": public.get("model", ""),
            "api_key": secret_data.get("llm_api_key", ""),
        }
        return LLMConfig.model_validate(data)

    def update_llm_config(
        self,
        *,
        api_format: str | None = None,
        api_surface: str | None = None,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> LLMConfig:
        current = self.load_llm_config()
        update: dict[str, Any] = {}
        if api_format is not None:
            update["api_format"] = api_format
        if api_surface is not None:
            update["api_surface"] = api_surface
        if reasoning_effort is not None:
            update["reasoning_effort"] = reasoning_effort
        if base_url is not None:
            update["base_url"] = base_url.strip()
        if model is not None:
            update["model"] = model.strip()
        if api_key is not None:
            update["api_key"] = api_key.strip()
        validated = current.model_copy(update=update)
        validated = LLMConfig.model_validate(validated.model_dump())
        with self._lock:
            self._write_json(
                self.config_path,
                {
                    "api_format": validated.api_format,
                    "api_surface": validated.api_surface,
                    "reasoning_effort": validated.reasoning_effort,
                    "base_url": validated.base_url,
                    "model": validated.model,
                },
            )
            secret_data = self._read_secrets()
            secret_data["llm_api_key"] = validated.api_key
            self._write_json(self.secrets_path, secret_data)
        return validated

    def create_session(self, title: str) -> dict[str, str]:
        now = _now()
        session_id = f"ops_{secrets.token_hex(8)}"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title[:120] or "New conversation", now, now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, str]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    def list_sessions(self) -> list[dict[str, str]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        if cursor.rowcount:
            self._delete_secret_namespace("sessions", session_id)
        return bool(cursor.rowcount)

    def claude_session_id(self, session_id: str) -> str | None:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT claude_session_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        value = row["claude_session_id"] if row is not None else None
        return str(value) if value else None

    def set_claude_session_id(self, session_id: str, claude_session_id: str | None) -> None:
        self.get_session(session_id)
        value = str(claude_session_id).strip() if claude_session_id else None
        if value is not None and not _CLAUDE_SESSION_ID_RE.fullmatch(value):
            raise ValueError("invalid Claude session id")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE sessions SET claude_session_id = ?, updated_at = ? WHERE id = ?",
                (value, _now(), session_id),
            )

    def create_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid IPC run id")
        now = _now()
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT id FROM runs WHERE session_id = ? AND status = 'running'",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise ValueError(f"IPC session already has an active run: {active['id']}")
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, status, started_at, updated_at
                    ) VALUES (?, ?, 'running', ?, ?)
                    """,
                    (run_id, session_id, now, now),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("IPC session already has an active run") from exc
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        raw_response = result.pop("response_json", None)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        result["response"] = None
        if raw_response:
            try:
                parsed = json.loads(raw_response)
                result["response"] = parsed if isinstance(parsed, dict) else None
            except (TypeError, ValueError):
                result["response"] = None
        return result

    def active_run(self, session_id: str) -> dict[str, Any] | None:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM runs
                WHERE session_id = ? AND status = 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self.get_run(str(row["id"])) if row is not None else None

    def request_run_cancel(self, session_id: str, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None or str(row["session_id"]) != session_id:
                raise KeyError(run_id)
            if row["status"] == "running":
                connection.execute(
                    "UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (_now(), run_id),
                )
        return self.get_run(run_id)

    def run_cancel_requested(self, run_id: str) -> bool:
        return bool(self.get_run(run_id)["cancel_requested"])

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("invalid terminal IPC run status")
        now = _now()
        response_json = (
            json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            if response is not None
            else None
        )
        safe_error = str(error).strip()[:12_000] if error else None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, response_json = ?, error = ?, updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, response_json, safe_error, now, now, run_id),
            )
            if not cursor.rowcount:
                existing = connection.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
                if existing is None:
                    raise KeyError(run_id)
        return self.get_run(run_id)

    def list_running_runs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE status = 'running' ORDER BY started_at"
            ).fetchall()
        return [self.get_run(str(row["id"])) for row in rows]

    def append_message(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        if role not in {"user", "assistant"}:
            raise ValueError("message role must be user or assistant")
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return {"id": cursor.lastrowid, "role": role, "content": content, "created_at": now}

    def list_messages(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at FROM (
                    SELECT id, role, content, created_at
                    FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_event(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        kind: str,
        label: str,
        text: str,
    ) -> dict[str, Any]:
        """Persist one safe, displayable IPC activity event.

        Stream events arrive after the service's secret redaction step.  Keep a
        bounded copy in the operations history so changing browser tabs or
        reconnecting does not make a completed task appear to have no logs.
        """

        self.get_session(session_id)
        now = _now()
        kind = str(kind or "event")[:80]
        label = str(label or "IPC")[:240]
        text = str(text or "")[:12_000]
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (session_id, run_id, kind, label, text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, run_id, kind, label, text, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return {
            "id": cursor.lastrowid,
            "run_id": run_id,
            "kind": kind,
            "label": label,
            "text": text,
            "created_at": now,
        }

    def list_events(self, session_id: str, *, limit: int = 1_000) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, label, text, created_at FROM (
                    SELECT id, kind, label, text, created_at
                    FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (session_id, max(1, min(int(limit), 5_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_run_events(
        self,
        session_id: str,
        run_id: str,
        *,
        after_id: int = 0,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, kind, label, text, created_at
                FROM events
                WHERE session_id = ? AND run_id = ? AND id > ?
                ORDER BY id LIMIT ?
                """,
                (session_id, run_id, max(0, int(after_id)), max(1, min(int(limit), 5_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_session_project(self, session_id: str, project_id: str) -> None:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO session_projects (session_id, project_id, created_at)
                VALUES (?, ?, ?)
                """,
                (session_id, str(project_id), _now()),
            )

    def list_session_projects(self, session_id: str) -> list[str]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id FROM session_projects WHERE session_id = ? ORDER BY created_at, project_id",
                (session_id,),
            ).fetchall()
        return [str(row["project_id"]) for row in rows]

    def save_session_secrets(self, session_id: str, values: dict[str, str]) -> None:
        self.get_session(session_id)
        self._save_secret_namespace("sessions", session_id, values)

    def session_secrets(self, session_id: str) -> dict[str, str]:
        self.get_session(session_id)
        return self._secret_namespace("sessions", session_id)

    def create_workflow(
        self,
        spec: PlatformWorkflowSpec,
        *,
        session_id: str | None = None,
        source: str = "manual",
        secrets_values: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if session_id is not None:
            self.get_session(session_id)
        now = _now()
        workflow_id = f"wf_{secrets.token_hex(8)}"
        spec_json, digest = _serialize_spec(spec)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflows (
                    id, session_id, source, name, spec_json, spec_digest,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (workflow_id, session_id, source, spec.name, spec_json, digest, now, now),
            )
        if session_id is not None:
            session_values = self._secret_namespace("sessions", session_id)
            selected = {
                name: session_values[name]
                for name in spec.required_secret_names()
                if name in session_values
            }
            if selected:
                self._save_secret_namespace("workflows", workflow_id, selected)
        if secrets_values:
            self.save_workflow_secrets(workflow_id, secrets_values)
        return self.get_workflow(workflow_id)

    def update_workflow(self, workflow_id: str, spec: PlatformWorkflowSpec) -> dict[str, Any]:
        spec_json, digest = _serialize_spec(spec)
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflows
                SET name = ?, spec_json = ?, spec_digest = ?, status = 'draft',
                    confirmed_digest = NULL, capability_hash = NULL, updated_at = ?
                WHERE id = ?
                """,
                (spec.name, spec_json, digest, now, workflow_id),
            )
        if not cursor.rowcount:
            raise KeyError(workflow_id)
        return self.get_workflow(workflow_id)

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        result = dict(row)
        result["spec"] = PlatformWorkflowSpec.model_validate_json(result.pop("spec_json"))
        result.pop("capability_hash", None)
        return result

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT id FROM workflows ORDER BY updated_at DESC").fetchall()
        return [self.get_workflow(row["id"]) for row in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        if cursor.rowcount:
            self._delete_secret_namespace("workflows", workflow_id)
        return bool(cursor.rowcount)

    def save_workflow_secrets(self, workflow_id: str, values: dict[str, str]) -> None:
        workflow = self.get_workflow(workflow_id)
        allowed = workflow["spec"].required_secret_names()
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown workflow secret names: {unknown}")
        self._save_secret_namespace("workflows", workflow_id, values)

    def workflow_secrets(self, workflow_id: str) -> dict[str, str]:
        self.get_workflow(workflow_id)
        return self._secret_namespace("workflows", workflow_id)

    def confirm_workflow(self, workflow_id: str) -> str:
        workflow = self.get_workflow(workflow_id)
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET status = 'confirmed', confirmed_digest = spec_digest,
                    capability_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (token_hash, now, workflow_id),
            )
        return token

    def revoke_workflow(self, workflow_id: str) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflows
                SET status = 'draft', confirmed_digest = NULL,
                    capability_hash = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, workflow_id),
            )
        if not cursor.rowcount:
            raise KeyError(workflow_id)
        return self.get_workflow(workflow_id)

    def verify_capability(self, workflow_id: str, token: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, spec_digest, confirmed_digest, capability_hash
                FROM workflows WHERE id = ?
                """,
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        valid = (
            row["status"] == "confirmed"
            and row["confirmed_digest"] == row["spec_digest"]
            and bool(row["capability_hash"])
            and hmac.compare_digest(row["capability_hash"], _hash_token(token))
        )
        if not valid:
            raise PermissionError("workflow capability is invalid or has been revoked")
        return self.get_workflow(workflow_id)

    def _read_secrets(self) -> dict[str, Any]:
        data = self._read_json(
            self.secrets_path,
            {"llm_api_key": "", "sessions": {}, "workflows": {}},
        )
        data.setdefault("llm_api_key", "")
        data.setdefault("sessions", {})
        data.setdefault("workflows", {})
        return data

    def _secret_namespace(self, group: str, object_id: str) -> dict[str, str]:
        with self._lock:
            data = self._read_secrets()
            values = data[group].get(object_id, {})
            return dict(values) if isinstance(values, dict) else {}

    def _save_secret_namespace(self, group: str, object_id: str, values: dict[str, str]) -> None:
        normalized: dict[str, str] = {}
        for name, value in values.items():
            secret = str(value)
            if not secret or len(secret) > 16_384:
                raise ValueError("secret values must contain between 1 and 16384 characters")
            if "\r" in secret or "\n" in secret:
                raise ValueError("secret values cannot contain newlines")
            normalized[validate_secret_name(name)] = secret
        with self._lock:
            data = self._read_secrets()
            namespace = data[group].setdefault(object_id, {})
            namespace.update(normalized)
            self._write_json(self.secrets_path, data)

    def _delete_secret_namespace(self, group: str, object_id: str) -> None:
        with self._lock:
            data = self._read_secrets()
            data[group].pop(object_id, None)
            self._write_json(self.secrets_path, data)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _restrict_file(temporary)
        os.replace(temporary, path)
        _restrict_file(path)


def _serialize_spec(spec: PlatformWorkflowSpec) -> tuple[str, str]:
    value = spec.model_dump(mode="json")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restrict_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
