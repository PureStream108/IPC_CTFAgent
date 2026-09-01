from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.core.config import LLMConfig
from backend.ops.models import PlatformWorkflowSpec, validate_secret_name
from backend.persistence.database import Database, PostgresDatabase
from psycopg.errors import UniqueViolation

_CLAUDE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]{8,128}$")
_TERMINAL_RUN_STATUSES = {"completed", "interrupted", "error", "abandoned"}

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SKILL_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_MAX_SKILLS = 32
# Budget for the combined block injected into IPC's context.
_SKILL_PROMPT_BUDGET = 24_000


class OpsStore:
    def __init__(
        self,
        root: str | Path,
        database: PostgresDatabase | None = None,
    ) -> None:
        self.root = Path(root) / "data" / "ops-agent"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.config_path = self.root / "config.json"
        self.secrets_path = self.root / "secrets.json"
        self.skills_dir = self.root / "skills"
        self.db = database or Database()
        self._owns_db = database is None
        self._lock = threading.RLock()
        self._configure_database()

    def _configure_database(self) -> None:
        if self._owns_db:
            self.db.configure()

    def _connect(self):
        return self.db.connect()

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
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (%s, %s, %s, %s)",
                (session_id, title[:120] or "New conversation", now, now),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict[str, str]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id = %s",
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
            cursor = connection.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
        if cursor.rowcount:
            self._delete_secret_namespace("sessions", session_id)
        return bool(cursor.rowcount)

    def claude_session_id(self, session_id: str) -> str | None:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT claude_session_id FROM sessions WHERE id = %s",
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
                "UPDATE sessions SET claude_session_id = %s, updated_at = %s WHERE id = %s",
                (value, _now(), session_id),
            )

    def create_run(self, session_id: str, run_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid IPC run id")
        now = _now()
        try:
            with self._lock, self._connect() as connection:
                active = connection.execute(
                    "SELECT id FROM runs WHERE session_id = %s AND status = 'running'",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise ValueError(f"IPC session already has an active run: {active['id']}")
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, session_id, status, started_at, updated_at
                    ) VALUES (%s, %s, 'running', %s, %s)
                    """,
                    (run_id, session_id, now, now),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = %s WHERE id = %s",
                    (now, session_id),
                )
        except UniqueViolation as exc:
            raise ValueError("IPC session already has an active run") from exc
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        raw_response = result.pop("response_json", None)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        result["response"] = None
        if isinstance(raw_response, dict):
            result["response"] = raw_response
        elif raw_response:
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
                WHERE session_id = %s AND status = 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self.get_run(str(row["id"])) if row is not None else None

    def request_run_cancel(self, session_id: str, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, status FROM runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            if row is None or str(row["session_id"]) != session_id:
                raise KeyError(run_id)
            if row["status"] == "running":
                connection.execute(
                    "UPDATE runs SET cancel_requested = TRUE, updated_at = %s WHERE id = %s",
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
                SET status = %s, response_json = %s::jsonb, error = %s, updated_at = %s, finished_at = %s
                WHERE id = %s AND status = 'running'
                """,
                (status, response_json, safe_error, now, now, run_id),
            )
            if not cursor.rowcount:
                existing = connection.execute("SELECT id FROM runs WHERE id = %s", (run_id,)).fetchone()
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
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (session_id, role, content, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE id = %s",
                (now, session_id),
            )
        row = cursor.fetchone()
        return {"id": row["id"], "role": role, "content": content, "created_at": now}

    def list_messages(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at FROM (
                    SELECT id, role, content, created_at
                    FROM messages WHERE session_id = %s ORDER BY id DESC LIMIT %s
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
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (session_id, run_id, kind, label, text, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE id = %s",
                (now, session_id),
            )
        row = cursor.fetchone()
        return {
            "id": row["id"],
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
                    FROM events WHERE session_id = %s ORDER BY id DESC LIMIT %s
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
                WHERE session_id = %s AND run_id = %s AND id > %s
                ORDER BY id LIMIT %s
                """,
                (session_id, run_id, max(0, int(after_id)), max(1, min(int(limit), 5_000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_session_project(self, session_id: str, project_id: str) -> None:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_projects (session_id, project_id, created_at)
                VALUES (%s, %s, %s) ON CONFLICT (session_id, project_id) DO NOTHING
                """,
                (session_id, str(project_id), _now()),
            )

    def list_session_projects(self, session_id: str) -> list[str]:
        self.get_session(session_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT project_id FROM session_projects WHERE session_id = %s ORDER BY created_at, project_id",
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
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, 'draft', %s, %s)
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
                SET name = %s, spec_json = %s::jsonb, spec_digest = %s, status = 'draft',
                    confirmed_digest = NULL, capability_hash = NULL, updated_at = %s
                WHERE id = %s
                """,
                (spec.name, spec_json, digest, now, workflow_id),
            )
        if not cursor.rowcount:
            raise KeyError(workflow_id)
        return self.get_workflow(workflow_id)

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM workflows WHERE id = %s", (workflow_id,)).fetchone()
        if row is None:
            raise KeyError(workflow_id)
        result = dict(row)
        raw_spec = result.pop("spec_json")
        result["spec"] = (
            PlatformWorkflowSpec.model_validate(raw_spec)
            if isinstance(raw_spec, dict)
            else PlatformWorkflowSpec.model_validate_json(raw_spec)
        )
        result.pop("capability_hash", None)
        return result

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT id FROM workflows ORDER BY updated_at DESC").fetchall()
        return [self.get_workflow(row["id"]) for row in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM workflows WHERE id = %s", (workflow_id,))
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

    # ---- operator-imported skills (SKILL.md) ----

    def list_skills(self) -> list[dict[str, Any]]:
        skills: list[dict[str, Any]] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            content = _read_skill_file(path)
            if content is None:
                continue
            meta = _parse_skill(content, path.stem)
            skills.append(
                {
                    "name": meta["name"],
                    "description": meta["description"],
                    "size": len(content.encode("utf-8")),
                }
            )
        return skills

    def import_skill(self, filename: str, content: str) -> dict[str, Any]:
        text = content.replace("\r\n", "\n").strip()
        if not text:
            raise ValueError("skill file is empty")
        if len(text.encode("utf-8")) > 256 * 1024:
            raise ValueError("skill file exceeds 256 KiB")
        meta = _parse_skill(text, Path(filename or "").stem)
        with self._lock:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            existing = {skill["name"] for skill in self.list_skills()}
            if meta["name"] not in existing and len(existing) >= _MAX_SKILLS:
                raise ValueError(f"skill limit reached ({_MAX_SKILLS})")
            target = self.skills_dir / f"{meta['name']}.md"
            target.write_text(text + "\n", encoding="utf-8")
            _restrict_file(target)
        return {"name": meta["name"], "description": meta["description"], "size": len(text.encode("utf-8"))}

    def delete_skill(self, name: str) -> bool:
        if not _SKILL_NAME_RE.fullmatch(name or ""):
            raise ValueError("invalid skill name")
        with self._lock:
            target = self.skills_dir / f"{name}.md"
            if not target.exists():
                return False
            target.unlink()
        return True

    def skills_prompt_text(self) -> str:
        """Render every imported skill as one bounded block for IPC's context."""

        parts: list[str] = []
        remaining = _SKILL_PROMPT_BUDGET
        for skill in self.list_skills():
            path = self.skills_dir / f"{skill['name']}.md"
            content = _read_skill_file(path)
            if content is None:
                continue
            body = content.strip()
            header = f"## skill: {skill['name']}"
            if skill["description"]:
                header += f" — {skill['description']}"
            block = f"{header}\n{body}"
            if len(block) > remaining:
                if remaining < 200:
                    break
                block = block[:remaining] + "\n[skill clipped]"
            parts.append(block)
            remaining -= len(block) + 2
            if remaining <= 0:
                break
        if not parts:
            return ""
        return (
            "The operator imported these SKILL.md playbooks. Follow them when relevant "
            "to the current task; treat their content as trusted operator guidance.\n\n"
            "<ipc-skills>\n" + "\n\n".join(parts) + "\n</ipc-skills>"
        )

    def confirm_workflow(self, workflow_id: str) -> str:
        self.get_workflow(workflow_id)
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET status = 'confirmed', confirmed_digest = spec_digest,
                    capability_hash = %s, updated_at = %s
                WHERE id = %s
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
                    capability_hash = NULL, updated_at = %s
                WHERE id = %s
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
                FROM workflows WHERE id = %s
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


def _read_skill_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_skill(content: str, fallback_name: str) -> dict[str, str]:
    """Extract name/description from SKILL.md YAML frontmatter.

    The name comes from frontmatter when present, otherwise from the file
    stem; either way it is normalized to a safe slug that also becomes the
    on-disk filename.
    """

    name = ""
    description = ""
    match = _SKILL_FRONTMATTER_RE.match(content)
    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            frontmatter = None
        if isinstance(frontmatter, dict):
            name = str(frontmatter.get("name") or "").strip()
            description = str(frontmatter.get("description") or "").strip()
    if not name:
        name = fallback_name
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    if not _SKILL_NAME_RE.fullmatch(slug):
        raise ValueError(
            "skill name must start with a letter or digit and contain only "
            "lowercase letters, numbers, underscores, or hyphens"
        )
    return {"name": slug, "description": description[:400]}
