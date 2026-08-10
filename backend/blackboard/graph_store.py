from __future__ import annotations

import json
import secrets
from typing import Any

from backend.blackboard import edge_store, node_store
from backend.blackboard.ids import (
    next_attachment_id,
    next_project_id,
    next_report_id,
    utcnow,
)
from backend.blackboard.models import (
    Agent,
    AgentLink,
    Attachment,
    Broadcast,
    Hint,
    ProjectDetail,
    ProjectMeta,
    ProjectReason,
    ProjectSummary,
    Report,
)
from backend.core.difficulty import normalize_difficulty
from backend.filename_util import numbered_filename


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        parsed = json.loads(value)
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []

# ---------- settings ----------


def get_timeouts(conn: Any) -> tuple[int, int]:
    row = conn.execute("SELECT intent_timeout, reason_timeout FROM settings WHERE id = 1").fetchone()
    return row["intent_timeout"], row["reason_timeout"]


# ---------- reason lease ----------


def reason_from_row(row: Any) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def clear_reason(conn: Any, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET reason_worker=NULL, reason_trigger=NULL, "
        "reason_started_at=NULL, reason_last_heartbeat_at=NULL WHERE id = %s",
        (project_id,),
    )


def claim_reason(conn: Any, project_id: str, worker: str, trigger: str) -> ProjectReason | None:
    row = conn.execute(
        "SELECT reason_worker FROM projects WHERE id = %s",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    current = row["reason_worker"]
    if current is not None and current != worker:
        return reason_from_row(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())
    now = utcnow()
    if current == worker:
        conn.execute(
            "UPDATE projects SET reason_trigger=%s, reason_last_heartbeat_at=%s, updated_at=%s WHERE id=%s",
            (trigger, now, now, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET reason_worker=%s, reason_trigger=%s, reason_started_at=%s, "
            "reason_last_heartbeat_at=%s, updated_at=%s WHERE id=%s",
            (worker, trigger, now, now, now, project_id),
        )
    return reason_from_row(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())


def heartbeat_reason(conn: Any, project_id: str, worker: str) -> ProjectReason | None:
    row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    if row is None or row["reason_worker"] != worker:
        return None
    now = utcnow()
    conn.execute(
        "UPDATE projects SET reason_last_heartbeat_at=%s, updated_at=%s WHERE id=%s",
        (now, now, project_id),
    )
    return reason_from_row(conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone())


def reason_holder(conn: Any, project_id: str) -> ProjectReason | None:
    row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
    return reason_from_row(row) if row is not None else None


def expire_reason_leases(conn: Any, timeout: int, project_id: str | None = None) -> None:
    query = """
        UPDATE projects SET reason_worker=NULL, reason_trigger=NULL,
            reason_started_at=NULL, reason_last_heartbeat_at=NULL
        WHERE reason_worker IS NOT NULL AND reason_last_heartbeat_at IS NOT NULL
          AND reason_last_heartbeat_at < now() - make_interval(secs => %s)
    """
    params: tuple = (timeout,)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = %s AND ", 1)
        params = (project_id, timeout)
    conn.execute(query, params)


# ---------- projects ----------


def project_meta(row: Any) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        external_id=row["external_id"],
        title=row["title"],
        category=row["category"],
        status=row["status"],
        postprocess_status=row.get("postprocess_status", "not_started"),
        terminal_reason=row.get("terminal_reason"),
        flag=row["flag"],
        flag_verified_at=row.get("flag_verified_at"),
        wp_path=row["wp_path"],
        log_filename=row["log_filename"],
        runtime_phase=row["runtime_phase"],
        runtime_error=row["runtime_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reason=reason_from_row(row),
    )


def get_project_row(conn: Any, project_id: str):
    return conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()


def create_project(
    conn: Any,
    title: str,
    origin: str,
    goal: str,
    category: str,
    hints: list[tuple[str, str]] | None = None,
    external_id: str | None = None,
) -> str:
    pid = next_project_id(conn)
    now = utcnow()
    used = [
        r["log_filename"]
        for r in conn.execute("SELECT log_filename FROM projects WHERE log_filename IS NOT NULL")
    ]
    log_filename = numbered_filename(title, ".jsonl", used, fallback=pid)
    conn.execute(
        "INSERT INTO projects (id, external_id, title, category, status, log_filename, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 'created', %s, %s, %s)",
        (pid, external_id, title, category, log_filename, now, now),
    )
    node_store.insert_fact(conn, pid, "origin", origin)
    node_store.insert_fact(conn, pid, "goal", goal)
    if hints:
        from backend.blackboard.ids import next_hint_id

        for content, creator in hints:
            hid = next_hint_id(conn, pid)
            conn.execute(
                "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (%s, %s, %s, %s, %s)",
                (hid, pid, content, creator, now),
            )
    # IPC and Diamond always exist from the start.
    add_agent(conn, pid, "ipc", "ipc", state="active")
    add_agent(conn, pid, "diamond", "diamond", state="idle")
    return pid


def touch_project(conn: Any, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = %s WHERE id = %s", (utcnow(), project_id))


def set_status(conn: Any, project_id: str, status: str) -> None:
    conn.execute(
        "UPDATE projects SET status = %s, updated_at = %s WHERE id = %s",
        (status, utcnow(), project_id),
    )


def set_runtime_phase(
    conn: Any,
    project_id: str,
    phase: str,
    error: str | None = None,
) -> None:
    """Publish a concise solver startup/runtime phase for UI and MCP clients."""

    safe_phase = str(phase or "idle").strip()[:80] or "idle"
    safe_error = str(error).strip()[:2_000] if error else None
    conn.execute(
        "UPDATE projects SET runtime_phase = %s, runtime_error = %s, updated_at = %s WHERE id = %s",
        (safe_phase, safe_error, utcnow(), project_id),
    )


def claim_project_lease(
    conn: Any,
    project_id: str,
    owner: str,
    *,
    token: str | None = None,
    timeout: int = 90,
) -> str | None:
    now = utcnow()
    if token:
        row = conn.execute(
            """
            UPDATE projects
            SET last_heartbeat_at = %s,
                lease_expires_at = %s::timestamptz + make_interval(secs => %s),
                updated_at = %s
            WHERE id = %s AND lease_owner = %s AND lease_token = %s
              AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
            RETURNING lease_token
            """,
            (now, now, timeout, now, project_id, owner, token),
        ).fetchone()
        return row["lease_token"] if row else None
    new_token = secrets.token_urlsafe(24)
    row = conn.execute(
        """
        UPDATE projects
        SET lease_owner = %s, lease_token = %s,
            lease_version = lease_version + 1,
            last_heartbeat_at = %s,
            lease_expires_at = %s::timestamptz + make_interval(secs => %s),
            updated_at = %s
        WHERE id = %s
          AND (
              lease_owner IS NULL
              OR (lease_expires_at IS NOT NULL AND lease_expires_at <= now())
          )
        RETURNING lease_token
        """,
        (owner, new_token, now, now, timeout, now, project_id),
    ).fetchone()
    return row["lease_token"] if row else None


def release_project_lease(conn: Any, project_id: str, owner: str, token: str) -> bool:
    return conn.execute(
        """
        UPDATE projects
        SET lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_heartbeat_at = NULL, updated_at = now()
        WHERE id = %s AND lease_owner = %s AND lease_token = %s
        """,
        (project_id, owner, token),
    ).rowcount == 1


def set_flag(conn: Any, project_id: str, flag: str) -> None:
    conn.execute(
        "UPDATE projects SET flag = %s, updated_at = %s WHERE id = %s", (flag, utcnow(), project_id)
    )


def set_wp_path(conn: Any, project_id: str, wp_path: str) -> None:
    conn.execute(
        "UPDATE projects SET wp_path = %s, updated_at = %s WHERE id = %s",
        (wp_path, utcnow(), project_id),
    )


def project_log_filename(conn: Any, project_id: str) -> str | None:
    row = conn.execute("SELECT log_filename FROM projects WHERE id = %s", (project_id,)).fetchone()
    return row["log_filename"] if row else None


def reset_project_counter_if_empty(conn: Any) -> None:
    row = conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
    if row is None:
        conn.execute("UPDATE counters SET value = 0 WHERE name = 'project'")


def delete_project(conn: Any, project_id: str) -> None:
    conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
    reset_project_counter_if_empty(conn)


# ---------- hints ----------


def create_hint(conn: Any, project_id: str, content: str, creator: str) -> Hint:
    from backend.blackboard.ids import next_hint_id

    now = utcnow()
    hid = next_hint_id(conn, project_id)
    conn.execute(
        "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (%s, %s, %s, %s, %s)",
        (hid, project_id, content, creator, now),
    )
    return Hint(id=hid, content=content, creator=creator, created_at=now)


def list_hints(conn: Any, project_id: str) -> list[Hint]:
    rows = conn.execute(
        "SELECT * FROM hints WHERE project_id = %s ORDER BY created_at, id", (project_id,)
    ).fetchall()
    return [Hint(id=r["id"], content=r["content"], creator=r["creator"], created_at=r["created_at"]) for r in rows]


def add_agent(
    conn: Any,
    project_id: str,
    name: str,
    role: str,
    state: str = "idle",
    start_fact_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO agents (project_id, name, role, state, start_fact_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, name) DO NOTHING",
        (project_id, name, role, state, start_fact_id, utcnow()),
    )


def set_agent_state(conn: Any, project_id: str, name: str, state: str) -> None:
    conn.execute(
        "UPDATE agents SET state = %s WHERE project_id = %s AND name = %s", (state, project_id, name)
    )


def list_agents(conn: Any, project_id: str) -> list[Agent]:
    rows = conn.execute(
        "SELECT * FROM agents WHERE project_id = %s ORDER BY created_at, name", (project_id,)
    ).fetchall()
    return [
        Agent(
            name=r["name"],
            role=r["role"],
            state=r["state"],
            start_fact_id=r["start_fact_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def active_member_names(conn: Any, project_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM agents WHERE project_id = %s AND role = 'member' AND state IN ('active','paused')",
        (project_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def add_link(conn: Any, project_id: str, src: str, dst: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO agent_links (project_id, src, dst, kind, created_at) VALUES (%s, %s, %s, %s, %s)",
        (project_id, src, dst, kind, utcnow()),
    )


def list_links(conn: Any, project_id: str) -> list[AgentLink]:
    rows = conn.execute(
        "SELECT * FROM agent_links WHERE project_id = %s ORDER BY id", (project_id,)
    ).fetchall()
    return [
        AgentLink(id=r["id"], src=r["src"], dst=r["dst"], kind=r["kind"], created_at=r["created_at"])
        for r in rows
    ]


def link_exists(conn: Any, project_id: str, src: str, dst: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_links WHERE project_id = %s AND src = %s AND dst = %s AND kind = %s",
        (project_id, src, dst, kind),
    ).fetchone()
    return row is not None


# ---------- reports ----------


def create_report(
    conn: Any,
    project_id: str,
    member: str,
    progress: str,
    difficulty: str,
    node_id: str | None,
    steps: list[str],
    directions: list[str],
    knowledge: list[str],
) -> Report:
    now = utcnow()
    rid = next_report_id(conn, project_id)
    normalized_difficulty = normalize_difficulty(difficulty)
    conn.execute(
        "INSERT INTO reports (id, project_id, member, node_id, progress, difficulty, "
        "steps_json, directions_json, knowledge_json, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)",
        (
            rid,
            project_id,
            member,
            node_id,
            progress,
            normalized_difficulty,
            json.dumps(steps, ensure_ascii=False),
            json.dumps(directions, ensure_ascii=False),
            json.dumps(knowledge, ensure_ascii=False),
            now,
        ),
    )
    return Report(
        id=rid,
        member=member,
        node_id=node_id,
        progress=progress,
        difficulty=normalized_difficulty,
        steps=steps,
        directions=directions,
        knowledge=knowledge,
        created_at=now,
    )


def list_reports(conn: Any, project_id: str) -> list[Report]:
    rows = conn.execute(
        "SELECT * FROM reports WHERE project_id = %s ORDER BY created_at, id", (project_id,)
    ).fetchall()
    return [
        Report(
            id=r["id"],
            member=r["member"],
            node_id=r["node_id"],
            progress=r["progress"],
            difficulty=r["difficulty"],
            steps=_json_list(r["steps_json"]),
            directions=_json_list(r["directions_json"]),
            knowledge=_json_list(r["knowledge_json"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------- attachments ----------


def create_attachment(conn: Any, project_id: str, filename: str, path: str) -> Attachment:
    now = utcnow()
    aid = next_attachment_id(conn, project_id)
    conn.execute(
        "INSERT INTO attachments (id, project_id, filename, path, created_at) VALUES (%s, %s, %s, %s, %s)",
        (aid, project_id, filename, path, now),
    )
    return Attachment(id=aid, filename=filename, path=path, created_at=now)


def list_attachments(conn: Any, project_id: str) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM attachments WHERE project_id = %s ORDER BY created_at, id", (project_id,)
    ).fetchall()
    return [
        Attachment(id=r["id"], filename=r["filename"], path=r["path"], created_at=r["created_at"])
        for r in rows
    ]


# ---------- broadcasts ----------


def add_broadcast(conn: Any, project_id: str | None, title: str, flag: str) -> Broadcast:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO broadcasts (project_id, title, flag, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (project_id, title, flag, now),
    )
    row = cur.fetchone()
    return Broadcast(id=row["id"], project_id=project_id, title=title, flag=flag, created_at=now)


def list_broadcasts(conn: Any, limit: int = 50) -> list[Broadcast]:
    rows = conn.execute(
        "SELECT * FROM broadcasts ORDER BY id DESC LIMIT %s", (limit,)
    ).fetchall()
    return [
        Broadcast(
            id=r["id"],
            project_id=r["project_id"],
            title=r["title"],
            flag=r["flag"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------- assembly ----------


def project_detail(conn: Any, project_id: str) -> ProjectDetail | None:
    row = get_project_row(conn, project_id)
    if row is None:
        return None
    return ProjectDetail(
        project=project_meta(row),
        facts=node_store.list_facts(conn, project_id),
        intents=edge_store.list_intents(conn, project_id),
        hints=list_hints(conn, project_id),
        agents=list_agents(conn, project_id),
        agent_links=list_links(conn, project_id),
        reports=list_reports(conn, project_id),
        attachments=list_attachments(conn, project_id),
    )


def project_summaries(conn: Any) -> list[ProjectSummary]:
    rows = conn.execute(
        """
        SELECT p.*,
            (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
            (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count,
            (SELECT COUNT(*) FROM agents WHERE project_id = p.id AND role = 'member') AS member_count
        FROM projects p ORDER BY p.created_at
        """
    ).fetchall()
    result: list[ProjectSummary] = []
    for row in rows:
        meta = project_meta(row)
        result.append(
            ProjectSummary(
                **meta.model_dump(),
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
                member_count=row["member_count"],
            )
        )
    return result
