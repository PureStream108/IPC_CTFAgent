from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.api.deps import get_state
from backend.blackboard import graph_store
from backend.core.state import AppState
from backend.platform.adapter import HttpJsonAdapter, PlatformAdapter
from backend.platform.mapping import FieldMapping, PlatformChallenge
from backend.platform.ret2shell import Ret2ShellAdapter, Ret2ShellClient, Ret2ShellError

router = APIRouter(prefix="/api/platform", tags=["platform"])


def safe_name(external_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", external_id).strip("._") or "challenge"
    return cleaned[:80]


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: FieldMapping


class ImportRequest(ChallengeRequest):
    select: list[str] | None = None


def _build_adapter(mapping: FieldMapping) -> PlatformAdapter:
    if mapping.platform == "ret2shell":
        client = Ret2ShellClient(base_url=mapping.list_url, game_id=mapping.game_id)
        return Ret2ShellAdapter(
            client,
            game_id=mapping.game_id or None,
            category_map=mapping.category_map,
        )
    return HttpJsonAdapter(mapping)


def _fetch(mapping: FieldMapping) -> tuple[PlatformAdapter, list[PlatformChallenge]]:
    adapter = _build_adapter(mapping)
    try:
        return adapter, adapter.fetch_challenges()
    except requests.RequestException as exc:
        raise HTTPException(502, f"platform request failed: {exc}") from exc
    except Ret2ShellError as exc:
        raise HTTPException(502, f"ret2shell request failed: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/challenges")
def preview_challenges(body: ChallengeRequest):
    _, challenges = _fetch(body.mapping)
    return {"challenges": [challenge.model_dump() for challenge in challenges]}


@router.post("/import", status_code=201)
def import_challenges(body: ImportRequest, state: AppState = Depends(get_state)):
    adapter, challenges = _fetch(body.mapping)
    by_id = {challenge.external_id: challenge for challenge in challenges}
    if body.select is None:
        selected = challenges
    else:
        missing = [external_id for external_id in body.select if external_id not in by_id]
        if missing:
            raise HTTPException(400, f"unknown external_id values: {missing}")
        selected = [by_id[external_id] for external_id in body.select]

    # Phase 1 downloads every attachment while NO database connection is
    # open.  Holding the write transaction across network downloads starved
    # the UI polling loop (and itself) with "database is locked" on large
    # imports.  Phase 2 then only performs fast local writes.
    staging_root = state.root / "staging" / f"import_{int(time.time() * 1000)}"
    staged: dict[str, list[Path]] = {}
    try:
        for challenge in selected:
            staged[challenge.external_id] = adapter.download_attachments(
                challenge,
                staging_root / safe_name(challenge.external_id),
            )
    except requests.RequestException as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise HTTPException(502, f"attachment download failed: {exc}") from exc
    except Ret2ShellError as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise HTTPException(502, f"ret2shell attachment download failed: {exc}") from exc
    except (OSError, TypeError, ValueError) as exc:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc

    imported: list[dict[str, str]] = []
    created_project_ids: list[str] = []
    try:
        with state.db.connect() as conn:
            for challenge in selected:
                # Mirror the ops workflow importer: the challenge statement
                # (connection commands, flag format) rides in the origin fact
                # so the Member context's challenge_description picks it up.
                origin = body.mapping.list_url
                if challenge.description:
                    origin = f"{origin}\n\n{challenge.description}"
                project_id = graph_store.create_project(
                    conn,
                    challenge.title,
                    origin,
                    "capture the flag",
                    challenge.category,
                    external_id=challenge.external_id,
                    platform=body.mapping.platform,
                )
                created_project_ids.append(project_id)
                attachment_dir = state.attachments_dir(project_id)
                attachment_dir.mkdir(parents=True, exist_ok=True)
                for path in staged.get(challenge.external_id, []):
                    target = attachment_dir / path.name
                    shutil.move(str(path), str(target))
                    graph_store.create_attachment(
                        conn,
                        project_id,
                        target.name,
                        str(target),
                    )
                imported.append(
                    {
                        "external_id": challenge.external_id,
                        "project_id": project_id,
                        "title": challenge.title,
                        "category": challenge.category,
                    }
                )
    except (OSError, TypeError, ValueError) as exc:
        for project_id in created_project_ids:
            shutil.rmtree(state.projects_dir / project_id, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    for item in imported:
        state.logger.project(
            "platform_project_imported",
            item["project_id"],
            external_id=item["external_id"],
            title=item["title"],
            category=item["category"],
        )
    return {"imported": imported}
