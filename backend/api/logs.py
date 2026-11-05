from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.api.project_groups import competition_project_ids
from backend.blackboard import graph_store
from backend.core.archive import list_archived_projects, project_archive_id
from backend.core.redaction import redact_object
from backend.core.state import AppState
from backend.filename_util import numbered_filename

router = APIRouter(tags=["logs"])
LOG_GROUPS = (
    ("project", "project_log"),
    ("llm", "llm_log"),
    ("tool", "tool_log"),
    ("memory", "memory_log"),
)
LOG_KINDS = tuple(kind for kind, _ in LOG_GROUPS)
ARCHIVE_PREFIX = "archive:"


class LogToggle(BaseModel):
    enabled: bool



_GET_STATE_DEPENDENCY = Depends(get_state)


def live_log_path(state: AppState, kind: str, filename: str):
    """Resolve one live project log file without trusting caller paths."""

    return state.logger.root / state.logger.KINDS[kind] / filename


def export_log_path(state: AppState, kind: str, filename: str):
    """Resolve one exported/archived log file inside the export directory."""

    return state.log_export_dir / state.logger.KINDS[kind] / filename


def project_log_rows(conn) -> list[dict]:
    """Read only the project columns the log catalog needs.

    graph_store.project_summaries fans out into facts/intents/hints/agents
    subqueries; the log catalog only needs identity, title, status, the log
    filename and the flag used for redaction.
    """

    rows = conn.execute(
        """
        SELECT id, title, status, created_at, log_filename, flag
        FROM projects
        ORDER BY created_at, id
        """
    ).fetchall()
    return [
        {
            "project_id": str(row["id"]),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "log_filename": str(row["log_filename"] or "") or f"{row['id']}.jsonl",
            "flag": row["flag"],
        }
        for row in rows
    ]


def _entry_count(state: AppState, kind: str, filename: str) -> int:
    return state.logger.count_file(live_log_path(state, kind, filename))


def _archive_entry_count(state: AppState, archive: dict, kind: str) -> int:
    counts = archive.get("log_counts")
    if isinstance(counts, dict):
        value = counts.get(kind)
        if isinstance(value, int) and value >= 0:
            return value
    return state.logger.count_file(export_log_path(state, kind, str(archive["log_filename"])))


def _competition_filter(state: AppState, competition_id: str) -> set[str] | None:
    text = str(competition_id or "").strip()
    if not text:
        return None
    try:
        with state.db.connect() as conn:
            return competition_project_ids(conn, text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="competition not found") from exc


@router.get("/logs/status")
def log_status(state: AppState = _GET_STATE_DEPENDENCY):
    return {"enabled": state.logger.enabled}


@router.put("/logs/status")
def set_log_status(body: LogToggle, state: AppState = _GET_STATE_DEPENDENCY):
    state.logger.set_enabled(body.enabled)
    state.config.log_enabled = body.enabled
    state.save_config()
    return {"enabled": state.logger.enabled}


@router.get("/logs/projects")
def read_project_logs(
    limit: int = 500,
    include_entries: bool = True,
    competition_id: str = "",
    state: AppState = _GET_STATE_DEPENDENCY,
):
    """Return the log catalog.

    include_entries=true keeps the historic payload (up to limit tail entries
    per kind).  include_entries=false returns catalog metadata only: real entry
    counts per kind, no entry bodies, no per-entry redaction, and projects whose
    four logs are empty are dropped server-side.
    """

    allowed = _competition_filter(state, competition_id)
    with state.db.connect() as conn:
        projects = project_log_rows(conn)
    if allowed is not None:
        projects = [project for project in projects if project["project_id"] in allowed]

    logs = []
    live_archive_ids: set[str] = set()
    for project in projects:
        live_archive_ids.add(project_archive_id(project["project_id"], project["created_at"]))
        item = {
            "project_id": project["project_id"],
            "title": project["title"],
            "status": project["status"],
            "archived": False,
        }
        entry_count = 0
        for kind, key in LOG_GROUPS:
            filename = project["log_filename"]
            count = _entry_count(state, kind, filename)
            entry_count += count
            if include_entries:
                item[key] = {
                    "filename": filename,
                    "entries": [
                        redact_object(entry, project["flag"])
                        for entry in state.logger.read_log(kind, project["project_id"], limit)
                    ],
                }
            else:
                item[key] = {"filename": filename, "count": count}
        item["entry_count"] = entry_count
        if include_entries or entry_count:
            logs.append(item)

    for archive in list_archived_projects(state):
        if archive["archive_id"] in live_archive_ids:
            continue
        source_project_id = str(archive["project_id"])
        if allowed is not None and source_project_id not in allowed:
            continue
        item = {
            "project_id": f"{ARCHIVE_PREFIX}{archive['archive_id']}",
            "source_project_id": source_project_id,
            "title": archive["title"],
            "status": "solved",
            "archived": True,
        }
        entry_count = 0
        for kind, key in LOG_GROUPS:
            count = _archive_entry_count(state, archive, kind)
            entry_count += count
            if include_entries:
                with state.db.connect() as conn:
                    source_row = graph_store.get_project_row(conn, source_project_id)
                secret = source_row["flag"] if source_row else None
                item[key] = {
                    "filename": archive["log_filename"],
                    "entries": [
                        redact_object(entry, secret)
                        for entry in state.logger.read_file(
                            export_log_path(state, kind, str(archive["log_filename"])), limit
                        )
                    ],
                }
            else:
                item[key] = {"filename": archive["log_filename"], "count": count}
        item["entry_count"] = entry_count
        if include_entries or entry_count:
            logs.append(item)
    return {
        "logs": logs
    }


@router.post("/logs/derive")
def derive_project_logs(state: AppState = _GET_STATE_DEPENDENCY):
    with state.db.connect() as conn:
        projects = graph_store.project_summaries(conn)
    target = state.log_export_dir
    target.mkdir(parents=True, exist_ok=True)
    files: dict[str, list[str]] = {
        state.logger.KINDS[kind]: [] for kind, _ in LOG_GROUPS
    }
    folders = {
        kind: target / state.logger.KINDS[kind]
        for kind, _ in LOG_GROUPS
    }
    for kind, _ in LOG_GROUPS:
        folders[kind].mkdir(parents=True, exist_ok=True)

    with state.export_lock:
        used = {
            path.name
            for folder in folders.values()
            for path in folder.glob("*.log")
        }
        for project in projects:
            contents = {
                kind: state.logger.jsonl_text(
                    state.logger.read_log(kind, project.id, None)
                )
                for kind, _ in LOG_GROUPS
            }
            while True:
                filename = numbered_filename(
                    project.title, ".log", used, fallback=project.id
                )
                created = []
                try:
                    for kind, _ in LOG_GROUPS:
                        path = folders[kind] / filename
                        with path.open("x", encoding="utf-8") as fh:
                            fh.write(contents[kind])
                        created.append(path)
                except FileExistsError:
                    for path in created:
                        path.unlink(missing_ok=True)
                    used.add(filename)
                    continue
                except Exception:
                    for path in created:
                        path.unlink(missing_ok=True)
                    raise

                used.add(filename)
                for kind, _ in LOG_GROUPS:
                    files[state.logger.KINDS[kind]].append(filename)
                break
    return {"dir": str(target), "files": files}




def _normalize_kind(kind: str) -> str:
    text = str(kind or "").strip()
    return text if text in LOG_KINDS else "project"


def _archive_by_id(state: AppState, archive_id: str) -> dict:
    for archive in list_archived_projects(state):
        if archive["archive_id"] == archive_id:
            return archive
    raise HTTPException(status_code=404, detail="archive not found")


@router.get("/logs/{project_id}")
def read_logs(project_id: str, kind: str = "project", limit: int = Query(default=500, ge=1, le=1000),
              after: int | None = Query(default=None, ge=0), file_id: str = "", state: AppState = _GET_STATE_DEPENDENCY):
    kind = _normalize_kind(kind)
    if project_id.startswith(ARCHIVE_PREFIX):
        archive = _archive_by_id(state, project_id[len(ARCHIVE_PREFIX):])
        with state.db.connect() as conn:
            row = graph_store.get_project_row(conn, str(archive["project_id"]))
        secret = row["flag"] if row else None
        path = export_log_path(state, kind, str(archive["log_filename"]))
        if after is not None:
            page = state.logger.read_file_since(path, after=after, limit=limit, file_id=file_id)
            page["entries"] = [redact_object(entry, secret) for entry in page["entries"]]
            return page
        return {"entries": [redact_object(entry, secret) for entry in state.logger.read_file(path, limit)]}
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
    secret = row["flag"] if row else None
    if after is not None:
        page = state.logger.read_since(kind, project_id, after=after, limit=limit, file_id=file_id)
        page["entries"] = [redact_object(entry, secret) for entry in page["entries"]]
        return page
    return {"entries": [redact_object(entry, secret) for entry in state.logger.read_log(kind, project_id, limit)]}
