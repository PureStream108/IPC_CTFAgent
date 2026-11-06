from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def competition_scope(identity: str) -> str:
    text = str(identity or "").strip()
    if not text:
        return ""
    if "://" in text:
        parts = urlsplit(text)
        scope = f"{parts.netloc}{parts.path}"
    else:
        scope = text
    for marker in ("/team/", "/account/"):
        index = scope.find(marker)
        if index > 0:
            scope = scope[:index]
    return scope.strip("/")


def _workflow_spec(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def project_competition_groups(conn: Any) -> list[dict]:
    groups: dict[str, dict] = {}

    def register(
        project_id: str | None,
        workflow_id: str | None,
        *,
        name: str = "",
        platform: str = "",
        identity: str = "",
    ) -> None:
        if not project_id or not workflow_id:
            return
        key = f"workflow:{workflow_id}"
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "id": key,
                "workflow_id": str(workflow_id),
                "label": "",
                "platform": "",
                "project_ids": [],
                "_identity": "",
            }
        if not group["label"] and name.strip():
            group["label"] = name.strip()
        if not group["platform"] and platform.strip():
            group["platform"] = platform.strip()
        if not group["_identity"] and identity.strip():
            group["_identity"] = identity.strip()
        if project_id not in group["project_ids"]:
            group["project_ids"].append(str(project_id))

    run_rows = conn.execute(
        """
        SELECT c.project_id, r.workflow_id, r.identity_key, r.config_snapshot,
               w.name AS workflow_name
        FROM competition_challenges c
        JOIN competition_runs r ON r.id = c.run_id
        LEFT JOIN workflows w ON w.id = r.workflow_id
        WHERE c.project_id IS NOT NULL
        """
    ).fetchall()
    import_rows = conn.execute(
        """
        SELECT wc.project_id, wc.workflow_id, w.name AS workflow_name,
               w.spec_json AS workflow_spec
        FROM workflow_challenges wc
        LEFT JOIN workflows w ON w.id = wc.workflow_id
        """
    ).fetchall()

    for row in run_rows:
        spec = _workflow_spec(_workflow_spec(row["config_snapshot"]).get("workflow_spec"))
        challenges = _workflow_spec(spec.get("challenges"))
        register(
            row["project_id"],
            row["workflow_id"],
            name=str(row["workflow_name"] or spec.get("name") or ""),
            platform=str(challenges.get("platform") or ""),
            identity=str(row["identity_key"] or ""),
        )
    for row in import_rows:
        spec = _workflow_spec(row["workflow_spec"])
        challenges = _workflow_spec(spec.get("challenges"))
        register(
            row["project_id"],
            row["workflow_id"],
            name=str(row["workflow_name"] or spec.get("name") or ""),
            platform=str(challenges.get("platform") or ""),
        )

    competitions = []
    for group in groups.values():
        label = group.pop("label") or ""
        identity = group.pop("_identity")
        group["label"] = label or competition_scope(identity) or f"Competition {group['workflow_id'][:8]}"
        group["project_ids"].sort()
        group["count"] = len(group["project_ids"])
        competitions.append(group)
    competitions.sort(key=lambda item: item["label"].casefold())
    return competitions


def competition_project_ids(conn: Any, competition_id: str) -> set[str]:
    prefix = "workflow:"
    workflow_id = competition_id.removeprefix(prefix)
    if not competition_id.startswith(prefix) or not workflow_id:
        raise KeyError("competition not found")
    if conn.execute("SELECT 1 FROM workflows WHERE id = %s", (workflow_id,)).fetchone() is None:
        raise KeyError("competition not found")
    group = next(
        (item for item in project_competition_groups(conn) if item["id"] == competition_id),
        None,
    )
    return set(group["project_ids"] if group else [])
