#!/usr/bin/env python3
"""Offline importer for IPC's pre-PostgreSQL data.

SQLite is intentionally used only as an input decoder in this standalone
utility. The application runtime never imports this module and never opens a
SQLite database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from alembic import command
from alembic.config import Config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.persistence.database import Database  # noqa: E402

GRAPH_TABLES = (
    "settings",
    "projects",
    "facts",
    "intents",
    "intent_sources",
    "hints",
    "agents",
    "agent_links",
    "reports",
    "attachments",
    "broadcasts",
    "counters",
    "scoped_counters",
)
OPS_TABLES = (
    "sessions",
    "messages",
    "runs",
    "events",
    "session_projects",
    "workflows",
)
SERIAL_TABLES = ("agent_links", "broadcasts", "messages", "events")
LEGACY_PROJECT_STATUS = {
    "created": "created",
    "running": "created",
    "flag_found": "flag_found",
    "wp_writing": "flag_found",
    "memory_writing": "flag_found",
    "completed": "flag_found",
    "solved": "flag_found",
    "stopped": "stopped",
    "timeout": "timeout",
    "infra_error": "infra_error",
    "failed": "failed",
}
JSON_COLUMNS = {
    ("reports", "steps_json"),
    ("reports", "directions_json"),
    ("reports", "knowledge_json"),
    ("memories", "tags"),
    ("runs", "response_json"),
    ("workflows", "spec_json"),
}

# Missing legacy columns must not be inserted as NULL. PostgreSQL only applies
# a column default when the column is omitted from the INSERT statement.
SERVER_DEFAULT_COLUMNS = {
    "settings": {"id", "intent_timeout", "reason_timeout"},
    "projects": {
        "category",
        "status",
        "postprocess_status",
        "runtime_phase",
        "lease_version",
    },
    "intents": {"lease_version", "retry_count"},
    "agents": {"state"},
    "agent_links": {"id"},
    "reports": {"steps_json", "directions_json", "knowledge_json"},
    "broadcasts": {"id"},
    "counters": {"value"},
    "scoped_counters": {"value"},
    "memories": {"tags", "source"},
    "mem_counter": {"value"},
    "messages": {"id"},
    "runs": {"cancel_requested"},
    "events": {"id"},
    "workflows": {"status"},
}

# These columns are NOT NULL and have no target-side default. A malformed
# legacy row should fail with an actionable message instead of an opaque
# PostgreSQL NotNullViolation.
REQUIRED_COLUMNS = {
    "projects": {"id", "title", "created_at", "updated_at"},
    "facts": {"id", "project_id", "description", "created_at"},
    "intents": {"id", "project_id", "description", "creator", "created_at"},
    "intent_sources": {"intent_id", "project_id", "fact_id"},
    "hints": {"id", "project_id", "content", "creator", "created_at"},
    "agents": {"project_id", "name", "role", "created_at"},
    "agent_links": {"project_id", "src", "dst", "kind", "created_at"},
    "reports": {"id", "project_id", "member", "progress", "difficulty", "created_at"},
    "attachments": {"id", "project_id", "filename", "path", "created_at"},
    "broadcasts": {"title", "flag", "created_at"},
    "counters": {"name"},
    "scoped_counters": {"project_id", "kind"},
    "memories": {"id", "category", "title", "content", "created_at"},
    "mem_counter": {"name"},
    "sessions": {"id", "title", "created_at", "updated_at"},
    "messages": {"session_id", "role", "content", "created_at"},
    "runs": {"id", "session_id", "status", "started_at", "updated_at"},
    "events": {"session_id", "kind", "label", "text", "created_at"},
    "session_projects": {"session_id", "project_id", "created_at"},
    "workflows": {
        "id",
        "source",
        "name",
        "spec_json",
        "spec_digest",
        "created_at",
        "updated_at",
    },
}

PROJECT_CHILD_TABLES = {
    "facts",
    "intents",
    "hints",
    "agents",
    "agent_links",
    "reports",
    "attachments",
    "scoped_counters",
}
SESSION_CHILD_TABLES = {"messages", "runs", "events", "session_projects"}


@dataclass
class ImportStats:
    imported: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, int] = field(default_factory=dict)

    def add(self, name: str, *, imported: int = 0, conflicts: int = 0) -> None:
        self.imported[name] = self.imported.get(name, 0) + imported
        self.conflicts[name] = self.conflicts.get(name, 0) + conflicts


class LegacyRowError(ValueError):
    """A legacy row cannot satisfy the target schema without inventing data."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _upgrade_database(database_url: str, revision: str) -> None:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.attributes["ipc_database_url"] = database_url
    command.upgrade(config, revision)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_entry(path: Path | None, kind: str) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.resolve()
    entry: dict[str, Any] = {
        "kind": kind,
        "path": str(resolved),
        "exists": resolved.exists(),
    }
    if resolved.is_file():
        entry.update({"size": resolved.stat().st_size, "sha256": _sha256(resolved)})
    elif resolved.is_dir():
        files = sorted(item for item in resolved.rglob("*") if item.is_file())
        digest = hashlib.sha256()
        size = 0
        for item in files:
            relative = item.relative_to(resolved).as_posix()
            file_digest = _sha256(item)
            digest.update(relative.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(file_digest.encode("ascii"))
            size += item.stat().st_size
        entry.update({"files": len(files), "size": size, "sha256": digest.hexdigest()})
    return entry


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _table_exists(connection, table):
        return []
    return [
        dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
    ]


def _json_text(value: Any, fallback: Any) -> str:
    if value in (None, ""):
        return json.dumps(fallback, ensure_ascii=False)
    if isinstance(value, (dict, list, bool, int, float)):
        return json.dumps(value, ensure_ascii=False)
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return json.dumps(json.loads(str(value)), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(fallback, ensure_ascii=False)


def _legacy_tags(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if not value:
        return "[]"
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return json.dumps(parsed, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    return json.dumps(
        [part.strip() for part in str(value).split(",") if part.strip()],
        ensure_ascii=False,
    )


def _legacy_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
            "none",
            "null",
        }
    return bool(value)


def _clean_timestamp(value: Any) -> Any:
    """Return NULL for blank legacy timestamps; preserve valid DB values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _coerce_counter(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _set_if_blank(row: dict[str, Any], key: str, value: Any) -> None:
    if row.get(key) is None or (isinstance(row.get(key), str) and not row[key].strip()):
        row[key] = value


def _project_row(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    # A few very early exports used ``name`` and omitted updated_at. Keep the
    # import useful while retaining the old timestamp whenever it exists.
    _set_if_blank(row, "id", str(row.get("external_id") or "").strip() or None)
    _set_if_blank(
        row, "title", row.get("name") or row.get("external_id") or row.get("id")
    )
    created_at = _clean_timestamp(row.get("created_at")) or _utcnow()
    row["created_at"] = created_at
    row["updated_at"] = _clean_timestamp(row.get("updated_at")) or created_at
    flag = str(row.get("flag") or "").strip() or None
    legacy = str(row.get("status") or "created").strip().lower()
    status = LEGACY_PROJECT_STATUS.get(legacy, "failed")
    if status == "flag_found" and not flag:
        status = "failed"
    row["status"] = status
    row["terminal_reason"] = (
        "legacy import: flag must be re-verified"
        if status == "flag_found"
        else (
            "legacy import: interrupted running project requeued"
            if legacy == "running"
            else None
        )
    )
    row["postprocess_status"] = "not_started"
    row["flag"] = flag
    row["flag_verified_at"] = None
    row["lease_owner"] = None
    row["lease_token"] = None
    row["lease_version"] = 0
    row["lease_expires_at"] = None
    row["last_heartbeat_at"] = None
    wp_path = str(row.get("wp_path") or "").strip()
    copied = (
        context.get("writeup_paths", {}).get(Path(wp_path).name) if wp_path else None
    )
    if copied:
        row["wp_path"] = str(copied)
    return row


def _created_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _intent_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    _set_if_blank(
        row, "description", row.get("content") or row.get("text") or row.get("id")
    )
    _set_if_blank(row, "creator", row.get("worker") or "legacy")
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    row["worker"] = None
    row["last_heartbeat_at"] = None
    row["lease_owner"] = None
    row["lease_token"] = None
    row["lease_version"] = 0
    row["lease_expires_at"] = None
    row["retry_count"] = 0
    return row


def _attachment_row(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    project_id = str(row.get("project_id") or "")
    filename = Path(str(row.get("filename") or row.get("path") or "")).name
    row["filename"] = filename
    candidate = (
        context["artifact_root"] / "projects" / project_id / "attachments" / filename
    )
    if candidate.is_file():
        row["path"] = str(candidate)
    elif not str(row.get("path") or "").strip():
        # Keep the row invalid rather than claiming a file exists when the
        # legacy attachment and copied artifact are both absent.
        row["path"] = None
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _report_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    for column in ("steps_json", "directions_json", "knowledge_json"):
        row[column] = _json_text(row.get(column), [])
    return row


def _memory_row(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    row["tags"] = _legacy_tags(row.get("tags"))
    if str(row.get("project_id") or "") not in context["project_ids"]:
        row["project_id"] = None
    row["category"] = str(row.get("category") or "lessons")
    row["source"] = str(row.get("source") or "legacy")
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _run_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    row["cancel_requested"] = _legacy_bool(row.get("cancel_requested"))
    status = str(row.get("status") or "abandoned").strip().lower()
    if status == "running":
        # No legacy worker can safely resume after the process boundary.
        row["status"] = "abandoned"
        row["error"] = row.get("error") or "legacy import: interrupted run abandoned"
    else:
        row["status"] = status
    if row.get("response_json") not in (None, ""):
        row["response_json"] = _json_text(row.get("response_json"), {})
    else:
        row["response_json"] = None
    row["started_at"] = _clean_timestamp(row.get("started_at")) or _utcnow()
    row["updated_at"] = _clean_timestamp(row.get("updated_at")) or row["started_at"]
    return row


def _session_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    row["updated_at"] = _clean_timestamp(row.get("updated_at")) or row["created_at"]
    _set_if_blank(row, "title", row.get("id") or "legacy session")
    return row


def _message_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _workflow_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    row["spec_json"] = _json_text(row.get("spec_json"), {})
    if str(row.get("session_id") or "") not in _context.get("session_ids", set()):
        row["session_id"] = None
    _set_if_blank(row, "source", "legacy")
    _set_if_blank(row, "name", row.get("id") or "legacy workflow")
    _set_if_blank(
        row, "spec_digest", hashlib.sha256(row["spec_json"].encode("utf-8")).hexdigest()
    )
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    row["updated_at"] = _clean_timestamp(row.get("updated_at")) or row["created_at"]
    return row


def _broadcast_row(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if str(row.get("project_id") or "") not in context["project_ids"]:
        row["project_id"] = None
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _event_row(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if str(row.get("run_id") or "") not in context.get("run_ids", set()):
        row["run_id"] = None
    _set_if_blank(row, "kind", "event")
    _set_if_blank(row, "label", row.get("kind") or "legacy")
    _set_if_blank(row, "text", "")
    row["created_at"] = _clean_timestamp(row.get("created_at")) or _utcnow()
    return row


def _counter_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    if row.get("value") is not None:
        row["value"] = _coerce_counter(row["value"])
    return row


def _serial_row(row: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
    """Accept integer SQLite ids, but let PostgreSQL allocate bad/missing ids."""
    value = row.get("id")
    if not _valid_serial_id(value):
        row.pop("id", None)
    else:
        try:
            row["id"] = int(value)
        except (TypeError, ValueError):
            row.pop("id", None)
    return row


def _valid_serial_id(value: Any) -> bool:
    try:
        return value not in (None, "") and int(value) > 0
    except (TypeError, ValueError):
        return False


def _sync_serial_sequence(target, table: str) -> None:
    """Advance a serial/identity sequence after explicit legacy ids."""
    sequence_row = target.execute(
        "SELECT pg_get_serial_sequence(%s, 'id') AS sequence_name",
        (table,),
    ).fetchone()
    sequence_name = sequence_row.get("sequence_name") if sequence_row else None
    if not sequence_name:
        return
    max_row = target.execute(f'SELECT MAX(id) AS max_id FROM "{table}"').fetchone()
    max_id = max_row.get("max_id") if max_row else None
    if max_id is None:
        return
    # pg_get_serial_sequence returns a server-quoted qualified identifier. It
    # cannot be passed as a value to SELECT, so use it only after PostgreSQL
    # has resolved it; table names here are fixed constants above.
    state = target.execute(
        f"SELECT last_value, is_called FROM {sequence_name}"
    ).fetchone()
    if (
        state
        and bool(state.get("is_called"))
        and int(state.get("last_value") or 0) >= int(max_id)
    ):
        return
    target.execute(
        "SELECT setval(%s::regclass, %s, true)", (sequence_name, int(max_id))
    )


def _refresh_context(target, context: dict[str, Any]) -> None:
    """Refresh parent keys inside the current transaction after each batch."""
    context["project_ids"] = {
        str(row["id"]) for row in target.execute('SELECT id FROM "projects"').fetchall()
    }
    context["intent_keys"] = {
        (str(row["id"]), str(row["project_id"]))
        for row in target.execute('SELECT id, project_id FROM "intents"').fetchall()
    }
    context["session_ids"] = {
        str(row["id"]) for row in target.execute('SELECT id FROM "sessions"').fetchall()
    }
    context["run_ids"] = {
        str(row["id"]) for row in target.execute('SELECT id FROM "runs"').fetchall()
    }


def _orphaned_parent(
    table: str, row: Mapping[str, Any], context: Mapping[str, Any]
) -> bool:
    project_id = str(row.get("project_id") or "")
    if table in PROJECT_CHILD_TABLES and project_id not in context.get(
        "project_ids", set()
    ):
        return True
    if table == "intent_sources":
        return (str(row.get("intent_id") or ""), project_id) not in context.get(
            "intent_keys", set()
        )
    if table in SESSION_CHILD_TABLES:
        return str(row.get("session_id") or "") not in context.get("session_ids", set())
    return False


TRANSFORMS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "projects": _project_row,
    "facts": _created_row,
    "intents": _intent_row,
    "hints": _created_row,
    "agents": _created_row,
    "agent_links": _created_row,
    "attachments": _attachment_row,
    "broadcasts": _broadcast_row,
    "reports": _report_row,
    "memories": _memory_row,
    "runs": _run_row,
    "events": _event_row,
    "sessions": _session_row,
    "messages": _message_row,
    "session_projects": _created_row,
    "counters": _counter_row,
    "scoped_counters": _counter_row,
    "mem_counter": _counter_row,
    "workflows": _workflow_row,
}

for _serial_table in SERIAL_TABLES:
    TRANSFORMS.setdefault(_serial_table, _serial_row)


TARGET_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": ("id", "intent_timeout", "reason_timeout"),
    "projects": (
        "id",
        "external_id",
        "title",
        "category",
        "status",
        "postprocess_status",
        "terminal_reason",
        "flag",
        "flag_verified_at",
        "wp_path",
        "log_filename",
        "runtime_phase",
        "runtime_error",
        "created_at",
        "updated_at",
        "reason_worker",
        "reason_trigger",
        "reason_started_at",
        "reason_last_heartbeat_at",
        "lease_owner",
        "lease_token",
        "lease_version",
        "lease_expires_at",
        "last_heartbeat_at",
    ),
    "facts": ("id", "project_id", "description", "created_at"),
    "intents": (
        "id",
        "project_id",
        "to_fact_id",
        "description",
        "creator",
        "worker",
        "last_heartbeat_at",
        "created_at",
        "concluded_at",
        "lease_owner",
        "lease_token",
        "lease_version",
        "lease_expires_at",
        "retry_count",
    ),
    "intent_sources": ("intent_id", "project_id", "fact_id"),
    "hints": ("id", "project_id", "content", "creator", "created_at"),
    "agents": ("project_id", "name", "role", "state", "start_fact_id", "created_at"),
    "agent_links": ("id", "project_id", "src", "dst", "kind", "created_at"),
    "reports": (
        "id",
        "project_id",
        "member",
        "node_id",
        "progress",
        "difficulty",
        "steps_json",
        "directions_json",
        "knowledge_json",
        "created_at",
    ),
    "attachments": ("id", "project_id", "filename", "path", "created_at"),
    "broadcasts": ("id", "project_id", "title", "flag", "created_at"),
    "counters": ("name", "value"),
    "scoped_counters": ("project_id", "kind", "value"),
    "memories": (
        "id",
        "category",
        "title",
        "content",
        "tags",
        "project_id",
        "source",
        "created_at",
    ),
    "mem_counter": ("name", "value"),
    "sessions": ("id", "title", "created_at", "updated_at", "claude_session_id"),
    "messages": ("id", "session_id", "role", "content", "created_at"),
    "runs": (
        "id",
        "session_id",
        "status",
        "cancel_requested",
        "response_json",
        "error",
        "started_at",
        "updated_at",
        "finished_at",
    ),
    "events": ("id", "session_id", "run_id", "kind", "label", "text", "created_at"),
    "session_projects": ("session_id", "project_id", "created_at"),
    "workflows": (
        "id",
        "session_id",
        "source",
        "name",
        "spec_json",
        "spec_digest",
        "status",
        "confirmed_digest",
        "capability_hash",
        "created_at",
        "updated_at",
    ),
}


CONFLICT_COLUMNS: dict[str, tuple[str, ...]] = {
    "settings": ("id",),
    "projects": ("id",),
    "facts": ("id", "project_id"),
    "intents": ("id", "project_id"),
    "intent_sources": ("intent_id", "project_id", "fact_id"),
    "hints": ("id", "project_id"),
    "agents": ("project_id", "name"),
    "agent_links": ("id",),
    "reports": ("id", "project_id"),
    "attachments": ("id", "project_id"),
    "broadcasts": ("id",),
    "counters": ("name",),
    "scoped_counters": ("project_id", "kind"),
    "memories": ("id",),
    "mem_counter": ("name",),
    "sessions": ("id",),
    "messages": ("id",),
    "runs": ("id",),
    "events": ("id",),
    "session_projects": ("session_id", "project_id"),
    "workflows": ("id",),
}


def _import_rows(
    target, table: str, rows: Iterable[Mapping[str, Any]], context: dict[str, Any]
) -> tuple[int, int]:
    columns = TARGET_COLUMNS[table]
    conflict = CONFLICT_COLUMNS[table]
    imported = 0
    conflicts = 0
    transform = TRANSFORMS.get(table)
    source_rows = list(rows)
    if table in SERIAL_TABLES:
        # Explicit ids must be inserted first; generated ids then start above
        # the imported range even when the legacy sequence was stale.
        source_rows.sort(key=lambda source: not _valid_serial_id(source.get("id")))
    for source in source_rows:
        row = dict(source)
        source_columns = set(row)
        if table == "settings":
            row.setdefault("id", 1)
            source_columns.add("id")
        if table in SERIAL_TABLES:
            row = _serial_row(row, context)
            if "id" not in row:
                _sync_serial_sequence(target, table)
        if transform is not None:
            row = transform(row, context)
        # A transform may synthesize fallback values for a field that was
        # absent in an old schema. Treat those exactly like server defaults
        # unless the source row actually carried the field.
        missing = sorted(
            column
            for column in REQUIRED_COLUMNS.get(table, set())
            if row.get(column) is None
        )
        if missing:
            raise LegacyRowError(
                f"{table} row cannot be imported; missing required column(s): {', '.join(missing)}"
            )
        if _orphaned_parent(table, row, context):
            conflicts += 1
            continue

        # Omit absent/NULL optional values. This preserves target defaults for
        # columns added after the legacy export and avoids explicit NULLs.
        insert_columns = tuple(
            column
            for column in columns
            if column in row
            and row[column] is not None
            and not (
                column not in source_columns
                and column in SERVER_DEFAULT_COLUMNS.get(table, set())
                and column not in REQUIRED_COLUMNS.get(table, set())
            )
        )
        if table == "settings" and "id" not in insert_columns:
            insert_columns = ("id",) + insert_columns
            row["id"] = 1
        if not insert_columns:
            conflicts += 1
            continue
        placeholders = [
            "%s::jsonb" if (table, column) in JSON_COLUMNS else "%s"
            for column in insert_columns
        ]
        sql = (
            f'INSERT INTO "{table}" ({", ".join(insert_columns)}) '
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({', '.join(conflict)}) DO NOTHING"
        )
        values = [row[column] for column in insert_columns]
        cursor = target.execute(sql, values)
        if cursor.rowcount:
            imported += 1
        else:
            conflicts += 1
    if table in SERIAL_TABLES:
        _sync_serial_sequence(target, table)
    return imported, conflicts


def _open_legacy(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _import_sqlite(
    database: Database,
    path: Path | None,
    tables: Iterable[str],
    context: dict[str, Any],
    stats: ImportStats,
) -> None:
    if path is None or not path.is_file():
        return
    with _open_legacy(path) as source, database.connect() as target:
        for table in tables:
            _refresh_context(target, context)
            values = _rows(source, table)
            if not values:
                continue
            imported, conflicts = _import_rows(target, table, values, context)
            stats.add(table, imported=imported, conflicts=conflicts)
        _refresh_context(target, context)


def _copy_file(source: Path, destination: Path) -> tuple[Path, bool]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        if _sha256(source) == _sha256(destination):
            return destination, False
        destination = destination.with_name(
            f"{destination.stem}.legacy-{_sha256(source)[:10]}{destination.suffix}"
        )
        if destination.is_file():
            return destination, False
    shutil.copy2(source, destination)
    return destination, True


def _copy_tree(
    source: Path | None,
    destination: Path,
    stats: ImportStats,
    key: str,
    suffixes: set[str] | None = None,
) -> dict[str, Path]:
    copied: dict[str, Path] = {}
    if source is None or not source.is_dir():
        return copied
    for item in sorted(path for path in source.rglob("*") if path.is_file()):
        if suffixes is not None and item.suffix.lower() not in suffixes:
            continue
        target, created = _copy_file(item, destination / item.relative_to(source))
        copied[item.name] = target
        stats.add(key, imported=int(created), conflicts=int(not created))
    return copied


def _parse_memory_markdown(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        marker = text.find("\n---", 4)
        if marker >= 0:
            parsed = yaml.safe_load(text[4:marker]) or {}
            if isinstance(parsed, dict):
                metadata = parsed
            body = text[marker + 4 :].lstrip("\r\n")
    lines = body.splitlines()
    title = next(
        (line[2:].strip() for line in lines if line.startswith("# ")), path.stem
    )
    if lines and lines[0].startswith("# "):
        content = "\n".join(lines[1:]).lstrip()
    else:
        content = body.strip()
    memory_id = str(metadata.get("id") or path.stem).strip()
    if not memory_id or not content:
        return None
    category = str(metadata.get("category") or path.parent.name or "lessons").strip()
    tags = metadata.get("tags") or []
    return {
        "id": memory_id,
        "category": category,
        "title": title,
        "content": content,
        "tags": _legacy_tags(tags),
        "project_id": str(metadata.get("project") or "").strip() or None,
        "source": str(metadata.get("source") or "legacy-markdown"),
        "created_at": str(
            metadata.get("created_at")
            or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        ),
    }


def _import_memory_markdown(
    database: Database, source: Path | None, context: dict[str, Any], stats: ImportStats
) -> None:
    if source is None or not source.is_dir():
        return
    values = [
        value
        for path in sorted(source.rglob("*.md"))
        if (value := _parse_memory_markdown(path))
    ]
    if values:
        with database.connect() as target:
            _refresh_context(target, context)
            imported, conflicts = _import_rows(target, "memories", values, context)
        stats.add("memory_markdown", imported=imported, conflicts=conflicts)


def _reset_sequences(database: Database) -> None:
    with database.connect() as connection:
        for table in SERIAL_TABLES:
            _sync_serial_sequence(connection, table)


def _start_run(database: Database, manifest: list[dict[str, Any]]) -> int:
    with database.connect() as connection:
        row = connection.execute(
            "INSERT INTO migration_runs (source_manifest, status) VALUES (%s::jsonb, 'running') RETURNING id",
            (json.dumps(manifest, ensure_ascii=False),),
        ).fetchone()
    return int(row["id"])


def _finish_run(
    database: Database, run_id: int, stats: ImportStats, error: str | None = None
) -> None:
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE migration_runs
            SET status = %s, imported_counts = %s::jsonb, conflict_counts = %s::jsonb,
                error = %s, finished_at = now()
            WHERE id = %s
            """,
            (
                "failed" if error else "completed",
                json.dumps(stats.imported, ensure_ascii=False),
                json.dumps(stats.conflicts, ensure_ascii=False),
                error,
                run_id,
            ),
        )


def _paths(args: argparse.Namespace) -> dict[str, Path | None]:
    root = args.legacy_root.resolve()

    def explicit(value: Path | None, default: Path) -> Path:
        return value.resolve() if value else default

    return {
        "graph_db": explicit(args.graph_db, root / "data" / "graph.db"),
        "memory_db": explicit(args.memory_db, root / "data" / "memory.db"),
        "ops_db": explicit(args.ops_db, root / "data" / "ops-agent" / "history.db"),
        "projects": explicit(args.projects_dir, root / "projects"),
        "writeups": explicit(args.writeups_dir, root / "wp"),
        "writeup_exports": explicit(args.writeup_export_dir, root / "exports" / "Wp"),
        "logs": explicit(args.logs_dir, root / "logs"),
        "log_exports": explicit(args.log_export_dir, root / "exports" / "logs"),
        "memory_markdown": explicit(args.memory_markdown_dir, root / "memory"),
        "memory_exports": explicit(args.memory_export_dir, root / "exports" / "memory"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, default=Path("."))
    parser.add_argument("--graph-db", type=Path)
    parser.add_argument("--memory-db", type=Path)
    parser.add_argument("--ops-db", type=Path)
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--writeups-dir", type=Path)
    parser.add_argument("--writeup-export-dir", type=Path)
    parser.add_argument("--logs-dir", type=Path)
    parser.add_argument("--log-export-dir", type=Path)
    parser.add_argument("--memory-markdown-dir", type=Path)
    parser.add_argument("--memory-export-dir", type=Path)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(os.environ.get("IPC_ARTIFACT_ROOT", "data/artifacts")),
    )
    parser.add_argument(
        "--database-url", default=os.environ.get("IPC_DATABASE_URL", "")
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the resolved manifest only"
    )
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    sources = _paths(args)
    artifact_root = args.artifact_root.resolve()
    manifest = [
        entry
        for key, path in sources.items()
        if (entry := _source_entry(path, key)) is not None
    ]
    result: dict[str, Any] = {
        "started_at": _utcnow(),
        "artifact_root": str(artifact_root),
        "sources": manifest,
    }
    if args.dry_run:
        result["status"] = "dry_run"
        return result

    database = Database(args.database_url or None)
    # Import against the base PostgreSQL schema so duplicate goal edges from a
    # legacy SQLite database can be loaded intact. The head migration then
    # audits and canonicalizes those rows before installing its unique index.
    _upgrade_database(database.dsn, "20260807_0001")
    database.open()
    stats = ImportStats()
    run_id = _start_run(database, manifest)
    result["migration_run_id"] = run_id
    error: str | None = None
    try:
        _copy_tree(
            sources["projects"], artifact_root / "projects", stats, "project_artifacts"
        )
        writeup_paths = _copy_tree(
            sources["writeups"],
            artifact_root / "writeups",
            stats,
            "writeup_artifacts",
            {".md"},
        )
        _copy_tree(
            sources["writeup_exports"],
            artifact_root / "exports" / "writeups",
            stats,
            "writeup_exports",
            {".md"},
        )
        _copy_tree(
            sources["logs"],
            artifact_root / "logs",
            stats,
            "log_artifacts",
            {".jsonl", ".json", ".log"},
        )
        _copy_tree(
            sources["log_exports"],
            artifact_root / "exports" / "logs",
            stats,
            "log_exports",
            {".jsonl", ".json", ".log"},
        )
        _copy_tree(
            sources["memory_markdown"],
            artifact_root / "exports" / "memory",
            stats,
            "memory_artifacts",
            {".md"},
        )
        _copy_tree(
            sources["memory_exports"],
            artifact_root / "exports" / "memory",
            stats,
            "memory_exports",
            {".md"},
        )
        with database.connect() as connection:
            project_ids = {
                row["id"]
                for row in connection.execute("SELECT id FROM projects").fetchall()
            }
        context = {
            "artifact_root": artifact_root,
            "writeup_paths": writeup_paths,
            "project_ids": project_ids,
        }
        _import_sqlite(database, sources["graph_db"], GRAPH_TABLES, context, stats)
        with database.connect() as connection:
            context["project_ids"] = {
                row["id"]
                for row in connection.execute("SELECT id FROM projects").fetchall()
            }
        _import_sqlite(
            database, sources["memory_db"], ("memories", "mem_counter"), context, stats
        )
        _import_memory_markdown(database, sources["memory_markdown"], context, stats)
        _import_memory_markdown(database, sources["memory_exports"], context, stats)
        _import_sqlite(database, sources["ops_db"], OPS_TABLES, context, stats)
        _reset_sequences(database)
        database.close()
        _upgrade_database(database.dsn, "head")
        result.update(
            {
                "status": "completed",
                "imported": stats.imported,
                "conflicts": stats.conflicts,
            }
        )
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:4000]
        result.update({"status": "failed", "error": error})
        raise
    finally:
        try:
            database.open()
            _finish_run(database, run_id, stats, error=error)
        finally:
            database.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
