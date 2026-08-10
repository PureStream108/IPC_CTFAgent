from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import sqlite3

import pytest

from scripts import migrate_legacy_data as migration


class _RecordingCursor:
    rowcount = 1

    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _RecordingTarget:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=()):
        self.calls.append((statement, tuple(params)))
        return _RecordingCursor()


def test_legacy_completed_project_requires_reverification(tmp_path):
    context = {"writeup_paths": {}, "artifact_root": tmp_path}
    migrated = migration._project_row(
        {
            "status": "completed",
            "flag": "flag{legacy}",
            "wp_path": "",
        },
        context,
    )

    assert migrated["status"] == "flag_found"
    assert migrated["flag_verified_at"] is None
    assert "re-verified" in migrated["terminal_reason"]


def test_legacy_completed_project_without_flag_is_not_solved(tmp_path):
    migrated = migration._project_row(
        {"status": "completed", "flag": "", "wp_path": ""},
        {"writeup_paths": {}, "artifact_root": tmp_path},
    )

    assert migrated["status"] == "failed"
    assert migrated["flag"] is None


def test_memory_markdown_parser_reads_frontmatter(tmp_path):
    source = tmp_path / "knowledge" / "mem_0001.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\n"
        "id: mem_0001\n"
        "category: knowledge\n"
        "tags: [web, ssti]\n"
        "project: proj_001\n"
        "source: diamond\n"
        "created_at: 2026-01-01T00:00:00Z\n"
        "---\n\n"
        "# Jinja sandbox\n\n"
        "Use object traversal carefully.\n",
        encoding="utf-8",
    )

    parsed = migration._parse_memory_markdown(source)

    assert parsed is not None
    assert parsed["title"] == "Jinja sandbox"
    assert parsed["tags"] == '["web", "ssti"]'
    assert parsed["project_id"] == "proj_001"


def test_artifact_copy_is_idempotent_and_preserves_conflicts(tmp_path):
    source = tmp_path / "source.md"
    target = tmp_path / "artifacts" / "writeups" / "source.md"
    source.write_text("first", encoding="utf-8")

    copied, created = migration._copy_file(source, target)
    same, created_again = migration._copy_file(source, target)
    source.write_text("second", encoding="utf-8")
    conflict, conflict_created = migration._copy_file(source, target)

    assert copied == same == target
    assert created is True and created_again is False
    assert conflict_created is True
    assert conflict.name.startswith("source.legacy-")
    assert target.read_text(encoding="utf-8") == "first"
    assert conflict.read_text(encoding="utf-8") == "second"


def test_dry_run_builds_manifest_without_postgres(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "logs" / "project_logs").mkdir(parents=True)
    (legacy / "logs" / "project_logs" / "one.jsonl").write_text(
        '{"event":"created"}\n', encoding="utf-8"
    )
    args = Namespace(
        legacy_root=legacy,
        graph_db=None,
        memory_db=None,
        ops_db=None,
        projects_dir=None,
        writeups_dir=None,
        writeup_export_dir=None,
        logs_dir=None,
        log_export_dir=None,
        memory_markdown_dir=None,
        memory_export_dir=None,
        artifact_root=tmp_path / "artifacts",
        database_url="",
        dry_run=True,
    )

    result = migration.run(args)

    assert result["status"] == "dry_run"
    log_source = next(item for item in result["sources"] if item["kind"] == "logs")
    assert log_source["files"] == 1
    assert log_source["sha256"]


def test_missing_sqlite_input_is_ignored_without_opening_or_creating_it(tmp_path):
    missing = tmp_path / "missing.db"

    class _UnexpectedDatabase:
        def connect(self):
            raise AssertionError("missing input should not open PostgreSQL")

    stats = migration.ImportStats()
    migration._import_sqlite(_UnexpectedDatabase(), missing, migration.GRAPH_TABLES, {}, stats)

    assert not missing.exists()
    assert stats.imported == {}


def test_upgrade_database_uses_repository_alembic_config(monkeypatch):
    calls = []

    class _Config:
        def __init__(self, path):
            self.path = path
            self.attributes = {}

    def _upgrade(config, revision):
        calls.append((config, revision))

    monkeypatch.setattr(migration, "Config", _Config)
    monkeypatch.setattr(migration.command, "upgrade", _upgrade)

    migration._upgrade_database(
        "postgresql://ipc:p%ss@127.0.0.1:5432/ipc", "20260807_0001"
    )

    config, revision = calls[0]
    assert config.path == str(migration.REPOSITORY_ROOT / "alembic.ini")
    assert config.attributes["ipc_database_url"] == (
        "postgresql://ipc:p%ss@127.0.0.1:5432/ipc"
    )
    assert revision == "20260807_0001"


def test_old_sqlite_schema_rows_are_read_with_only_their_actual_columns(tmp_path):
    source = tmp_path / "graph.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO projects VALUES (
                'proj_old', 'Old project', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            );
            """
        )

    with migration._open_legacy(source) as connection:
        row = migration._rows(connection, "projects")[0]

    assert set(row) == {"id", "title", "created_at", "updated_at"}


def test_import_rows_omits_columns_missing_from_old_schema_and_keeps_defaults():
    target = _RecordingTarget()
    context = {"artifact_root": Path("/tmp/artifacts"), "writeup_paths": {}, "project_ids": set()}

    imported, conflicts = migration._import_rows(
        target,
        "projects",
        [
            {
                "id": "proj_old",
                "title": "Old project",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ],
        context,
    )

    assert (imported, conflicts) == (1, 0)
    statement, _ = target.calls[0]
    assert '"category"' not in statement
    assert '"runtime_phase"' not in statement
    assert '"status"' not in statement


def test_import_rows_reports_missing_non_nullable_legacy_fields():
    target = _RecordingTarget()
    with pytest.raises(migration.LegacyRowError, match="projects row.*id, title"):
        migration._import_rows(
            target,
            "projects",
            [{"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}],
            {"artifact_root": Path("/tmp/artifacts"), "writeup_paths": {}, "project_ids": set()},
        )


def test_report_json_columns_use_server_defaults_when_absent_and_jsonb_cast_when_present():
    target = _RecordingTarget()
    migration._import_rows(
        target,
        "reports",
        [
            {
                "id": "report_old",
                "project_id": "proj_old",
                "member": "member",
                "progress": "partial",
                "difficulty": "low",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        {"artifact_root": Path("/tmp/artifacts"), "writeup_paths": {}, "project_ids": {"proj_old"}},
    )
    statement, _ = target.calls[0]
    assert "::jsonb" not in statement
    assert "steps_json" not in statement

    target = _RecordingTarget()
    migration._import_rows(
        target,
        "reports",
        [
            {
                "id": "report_json",
                "project_id": "proj_old",
                "member": "member",
                "progress": "partial",
                "difficulty": "low",
                "steps_json": '[{"step": 1}]',
                "directions_json": "not-json",
                "knowledge_json": "[]",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        {"artifact_root": Path("/tmp/artifacts"), "writeup_paths": {}, "project_ids": {"proj_old"}},
    )
    statement, params = target.calls[0]
    assert statement.count("::jsonb") == 3
    assert params[-4:-1] == ('[{"step": 1}]', "[]", "[]")
