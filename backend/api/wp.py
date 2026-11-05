from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.archive import list_archived_projects, project_archive_id
from backend.core.state import AppState
from backend.filename_util import numbered_filename

router = APIRouter(tags=["wp"])


def _completed_writeups(state: AppState) -> list[dict]:
    with state.db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, wp_path, updated_at, created_at
            FROM projects
            WHERE status IN ('solved','completed') AND wp_path IS NOT NULL
            ORDER BY created_at, id
            """
        ).fetchall()
    used: set[str] = set()
    out: list[dict] = []
    live_archive_ids: set[str] = set()
    for row in rows:
        path = Path(row["wp_path"])
        if not path.exists():
            continue
        filename = numbered_filename(row["title"], ".md", used, fallback=row["id"])
        used.add(filename)
        archive_id = project_archive_id(str(row["id"]), str(row["created_at"]))
        live_archive_ids.add(archive_id)
        out.append(
            {
                "project_id": row["id"],
                "title": row["title"],
                "filename": filename,
                "content": path.read_text(encoding="utf-8"),
                "updated_at": row["updated_at"],
                "archive_id": archive_id,
                "archived": False,
            }
        )
    for archive in list_archived_projects(state):
        if archive["archive_id"] in live_archive_ids:
            continue
        path = state.wp_export_dir / archive["wp_filename"]
        out.append(
            {
                "project_id": f"archive:{archive['archive_id']}",
                "source_project_id": archive["project_id"],
                "title": archive["title"],
                "filename": archive["wp_filename"],
                "content": path.read_text(encoding="utf-8"),
                "updated_at": archive["archived_at"],
                "archive_id": archive["archive_id"],
                "archived": True,
            }
        )
    return out


def _iso_mtime(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _content_etag(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _project_wp_record(
    state: AppState, project_id: str, *, include_content: bool = True
) -> dict:
    """Return one project's writeup and the metadata used by ETag/SSE clients.

    status is intentionally explicit so the HTTP layer can distinguish a
    missing project, a project without a writeup yet, and a writeup path whose
    file disappeared between polls. No database row is created or migrated.

    include_content=False performs only the row/stat work. The SSE loop uses it
    to avoid hashing a file whose mtime_ns and size have not changed.
    """
    with state.db.connect() as conn:
        row = graph_store.get_project_row(conn, project_id)
    if row is None:
        return {"status": "project_missing", "project_id": project_id}
    wp_path = row["wp_path"]
    if not wp_path:
        return {
            "status": "no_writeup",
            "project_id": project_id,
            "wp_path": "",
            "project_updated_at": str(row["updated_at"] or ""),
        }
    path = Path(wp_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {
            "status": "missing",
            "project_id": project_id,
            "wp_path": str(path),
            "project_updated_at": str(row["updated_at"] or ""),
        }
    except OSError:
        return {
            "status": "error",
            "error": "stat_failed",
            "project_id": project_id,
            "wp_path": str(path),
            "project_updated_at": str(row["updated_at"] or ""),
        }
    metadata = {
        "status": "ok",
        "project_id": project_id,
        "wp_path": str(path),
        "updated_at": _iso_mtime(stat.st_mtime),
        "mtime_ns": int(stat.st_mtime_ns),
        "size": int(stat.st_size),
    }
    if not include_content:
        return metadata
    try:
        content = path.read_text(encoding="utf-8")
    except PermissionError:
        return {
            "status": "error",
            "error": "permission_denied",
            "project_id": project_id,
            "wp_path": str(path),
            "project_updated_at": str(row["updated_at"] or ""),
        }
    except OSError:
        return {
            "status": "error",
            "error": "read_failed",
            "project_id": project_id,
            "wp_path": str(path),
            "project_updated_at": str(row["updated_at"] or ""),
        }
    return {
        **metadata,
        "content": content,
        "etag": _content_etag(content),
    }


def _wp_headers(record: dict) -> dict[str, str]:
    return {
        "ETag": f'"{record["etag"]}"',
        "Cache-Control": "no-cache",
        "X-WP-ETag": str(record["etag"]),
        "X-WP-Updated-At": str(record["updated_at"]),
    }


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = etag.strip('"')
    for candidate in header.split(","):
        token = candidate.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token.strip('"') == wanted:
            return True
    return False


def _stream_interval() -> float:
    try:
        value = float(os.environ.get("IPC_WP_STREAM_INTERVAL", "0.75"))
    except ValueError:
        value = 0.75
    return min(5.0, max(0.25, value))


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _public_record(record: dict) -> dict:
    return {
        "project_id": record["project_id"],
        "wp_path": record["wp_path"],
        "content": record["content"],
        "etag": record["etag"],
        "updated_at": record["updated_at"],
    }


class WPToggle(BaseModel):
    enabled: bool


_GET_STATE_DEPENDENCY = Depends(get_state)


@router.get("/wp/status")
def wp_status(state: AppState = _GET_STATE_DEPENDENCY):
    """Hot switch state for automatic writeups."""
    return {"enabled": bool(state.config.wp_enabled)}


@router.put("/wp/status")
def set_wp_status(body: WPToggle, state: AppState = _GET_STATE_DEPENDENCY):
    """Toggle writeup generation immediately and persist the choice.

    The postprocess worker reads the same config value for every claim, so an
    in-flight run starts or stops writing WP on the next job without a restart.
    """
    state.config.wp_enabled = bool(body.enabled)
    state.save_config()
    state.logger.project("wp_toggled", "startup", enabled=state.config.wp_enabled)
    return {"enabled": bool(state.config.wp_enabled)}


@router.get("/projects/{project_id}/wp")
def get_wp(
    project_id: str,
    request: Request,
    state: AppState = _GET_STATE_DEPENDENCY,
):
    record = _project_wp_record(state, project_id)
    if record["status"] == "project_missing":
        raise HTTPException(404, "Project not found")
    if record["status"] in {"no_writeup", "missing"}:
        raise HTTPException(404, "No writeup yet")
    if record["status"] != "ok":
        raise HTTPException(503, "Writeup is temporarily unavailable")
    headers = _wp_headers(record)
    if _etag_matches(request.headers.get("if-none-match"), str(record["etag"])):
        return Response(status_code=304, headers=headers)
    return Response(
        content=record["content"],
        media_type="text/markdown",
        headers=headers,
    )


async def _wp_event_stream(state: AppState, project_id: str, initial: dict):
    record = initial
    last_key = (record["wp_path"], record["mtime_ns"], record["size"])
    last_etag = ""
    interval = _stream_interval()
    last_keepalive = asyncio.get_running_loop().time()
    while True:
        if record["status"] == "ok" and record["etag"] != last_etag:
            last_etag = record["etag"]
            yield _sse("writeup", _public_record(record))
        elif record["status"] == "missing":
            yield _sse("missing", {"project_id": project_id, "wp_path": record.get("wp_path", "")})
            return
        elif record["status"] in {"no_writeup", "project_missing"}:
            yield _sse("missing", {"project_id": project_id, "wp_path": ""})
            return
        elif record["status"] == "error":
            yield _sse("error", {"type": record.get("error", "read_failed")})
            return

        now = asyncio.get_running_loop().time()
        if now - last_keepalive >= 15:
            last_keepalive = now
            yield ": keepalive\n\n"

        await asyncio.sleep(interval)
        candidate = await asyncio.to_thread(
            _project_wp_record, state, project_id, include_content=False
        )
        if candidate["status"] == "ok":
            key = (candidate["wp_path"], candidate["mtime_ns"], candidate["size"])
            if key == last_key:
                continue
            last_key = key
            candidate = await asyncio.to_thread(_project_wp_record, state, project_id)
        record = candidate


@router.get("/projects/{project_id}/wp/stream")
async def stream_wp(project_id: str, state: AppState = _GET_STATE_DEPENDENCY):
    initial = await asyncio.to_thread(_project_wp_record, state, project_id)
    if initial["status"] == "project_missing":
        raise HTTPException(404, "Project not found")
    if initial["status"] in {"no_writeup", "missing"}:
        raise HTTPException(404, "No writeup yet")
    if initial["status"] != "ok":
        raise HTTPException(503, "Writeup is temporarily unavailable")

    return StreamingResponse(
        _wp_event_stream(state, project_id, initial),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/wp/completed")
def completed_wp(state: AppState = _GET_STATE_DEPENDENCY):
    return {"writeups": _completed_writeups(state)}


@router.post("/wp/derive")
def derive_wp(state: AppState = _GET_STATE_DEPENDENCY):
    target = state.wp_export_dir
    target.mkdir(parents=True, exist_ok=True)
    writeups = _completed_writeups(state)
    files: list[str] = []
    with state.export_lock:
        used = {path.name for path in target.glob("*.md")}
        for item in writeups:
            while True:
                filename = numbered_filename(
                    item["title"], ".md", used, fallback=item["project_id"]
                )
                try:
                    with (target / filename).open("x", encoding="utf-8") as fh:
                        fh.write(item["content"])
                except FileExistsError:
                    used.add(filename)
                    continue
                used.add(filename)
                files.append(filename)
                break
    return {"dir": str(target), "files": files}


@router.get("/wp")
def list_wp(state: AppState = _GET_STATE_DEPENDENCY):
    files = sorted(state.wp_dir.glob("*.md"))
    return {"writeups": [f.name for f in files]}
