from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.blackboard import graph_store
from backend.core.logging_util import IPCLogger
from backend.core.wp_writer import validate_writeup
from backend.filename_util import safe_stem

_ARCHIVE_VERSION = 1
_LOG_KINDS = ("project", "llm", "tool", "memory")


def project_archive_id(project_id: str, created_at: str) -> str:
    digest = hashlib.sha256(
        f"{project_id}\0{created_at}".encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"{safe_stem(project_id, fallback='project', max_length=64)}-{digest}"


def archive_completed_project(
    state,
    project_id: str,
    *,
    connection=None,
) -> dict[str, Any]:
    """Write one idempotent, durable WP + log bundle for a completed project."""

    if connection is None:
        with state.db.connect() as own_connection:
            row = graph_store.get_project_row(own_connection, project_id)
    else:
        row = graph_store.get_project_row(connection, project_id)
    if row is None:
        raise ValueError(f"project not found: {project_id}")
    if row["status"] != "solved":
        raise ValueError(f"project {project_id} is not solved")
    wp_path = Path(str(row["wp_path"] or ""))
    if not wp_path.is_file():
        raise ValueError(f"project {project_id} does not have a readable writeup")
    writeup_errors = validate_writeup(
        wp_path.read_text(encoding="utf-8"),
        expected_flag=str(row["flag"] or "") or None,
        require_complete=True,
    )
    if writeup_errors:
        raise ValueError(
            f"project {project_id} does not have a valid final writeup: {'; '.join(writeup_errors)}"
        )

    archive_id = project_archive_id(str(row["id"]), str(row["created_at"]))
    manifest_dir = state.log_export_dir / ".ipc-archives"
    manifest_path = manifest_dir / f"{archive_id}.json"
    with state.export_lock:
        existing = _read_manifest(manifest_path)
        if existing is not None:
            return existing

        state.wp_export_dir.mkdir(parents=True, exist_ok=True)
        folders = {
            kind: state.log_export_dir / state.logger.KINDS[kind]
            for kind in _LOG_KINDS
        }
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        manifest_dir.mkdir(parents=True, exist_ok=True)

        stem = _available_stem(
            str(row["title"]),
            str(row["id"]),
            state.wp_export_dir,
            tuple(folders.values()),
        )
        wp_filename = f"{stem}.md"
        log_filename = f"{stem}.log"
        created: list[Path] = []
        try:
            wp_target = state.wp_export_dir / wp_filename
            with wp_target.open("x", encoding="utf-8") as handle:
                handle.write(wp_path.read_text(encoding="utf-8"))
            created.append(wp_target)
            for kind, folder in folders.items():
                target = folder / log_filename
                with target.open("x", encoding="utf-8") as handle:
                    handle.write(
                        IPCLogger.jsonl_text(state.logger.read_log(kind, project_id, None))
                    )
                created.append(target)

            manifest = {
                "version": _ARCHIVE_VERSION,
                "archive_id": archive_id,
                "project_id": str(row["id"]),
                "title": str(row["title"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "archived_at": datetime.now(UTC).isoformat(),
                "wp_filename": wp_filename,
                "log_filename": log_filename,
            }
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return manifest
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            raise


def list_archived_projects(state) -> list[dict[str, Any]]:
    manifest_dir = state.log_export_dir / ".ipc-archives"
    if not manifest_dir.is_dir():
        return []
    archives: list[dict[str, Any]] = []
    for path in sorted(manifest_dir.glob("*.json")):
        value = _read_manifest(path)
        if value is None:
            continue
        wp_path = state.wp_export_dir / str(value["wp_filename"])
        log_filename = str(value["log_filename"])
        if not wp_path.is_file():
            continue
        if not all(
            (state.log_export_dir / state.logger.KINDS[kind] / log_filename).is_file()
            for kind in _LOG_KINDS
        ):
            continue
        archives.append(value)
    archives.sort(key=lambda item: (str(item.get("created_at", "")), str(item["archive_id"])))
    return archives


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != _ARCHIVE_VERSION:
        return None
    required = {
        "archive_id",
        "project_id",
        "title",
        "created_at",
        "archived_at",
        "wp_filename",
        "log_filename",
    }
    if not required <= value.keys():
        return None
    for key in required:
        if not isinstance(value.get(key), str) or not value[key]:
            return None
    return value


def _available_stem(
    title: str,
    project_id: str,
    wp_dir: Path,
    log_dirs: tuple[Path, ...],
) -> str:
    base = safe_stem(title, fallback=project_id)
    used_wp = {path.name.casefold() for path in wp_dir.glob("*.md")}
    used_logs = {
        path.name.casefold()
        for folder in log_dirs
        for path in folder.glob("*.log")
    }
    counter = 0
    while True:
        stem = base if counter == 0 else f"{base}{counter:02d}"
        if f"{stem}.md".casefold() not in used_wp and f"{stem}.log".casefold() not in used_logs:
            return stem
        counter += 1
