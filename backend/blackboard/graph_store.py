from __future__ import annotations

import json
import sqlite3

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
    Fact,
    Hint,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectReason,
    ProjectSummary,
    Report,
)

# ---------- settings ----------


def get_timeouts(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute("SELECT intent_timeout, reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"], row["reason_timeout"]


def set_timeouts(conn: sqlite3.Connection, intent_timeout: int, reason_timeout: int) -> None:
    conn.execute(
        "UPDATE settings SET intent_timeout = ?, reason_timeout = ? WHERE rowid = 1",
        (intent_timeout, reason_timeout),
    )


# ---------- reason lease ----------


def reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def clear_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        "UPDATE projects SET reason_worker=NULL, reason_trigger=NULL, "
        "reason_started_at=NULL, reason_last_heartbeat_at=NULL WHERE id = ?",
        (project_id,),
    )


def expire_reason_leases(conn: sqlite3.Connection, timeout: int, project_id: str | None = None) -> None:
    now = utcnow()
    query = """
        UPDATE projects SET reason_worker=NULL, reason_trigger=NULL,
            reason_started_at=NULL, reason_last_heartbeat_at=NULL
        WHERE reason_worker IS NOT NULL AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


# ---------- projects ----------


def project_meta(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        category=row["category"],
        status=row["status"],
        flag=row["flag"],
        wp_path=row["wp_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        reason=reason_from_row(row),
    )


def get_project_row(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def create_project(
    conn: sqlite3.Connection,
    title: str,
    origin: str,
    goal: str,
    category: str,
    hints: list[tuple[str, str]] | None = None,
) -> str:
    """Create a project with origin/goal facts and the IPC + Diamond agents."""
    pid = next_project_id(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (id, title, category, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'created', ?, ?)",
        (pid, title, category, now, now),
    )
    node_store.insert_fact(conn, pid, "origin", origin)
    node_store.insert_fact(conn, pid, "goal", goal)
    if hints:
        from backend.blackboard.ids import next_hint_id

        for content, creator in hints:
            hid = next_hint_id(conn, pid)
            conn.execute(
                "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                (hid, pid, content, creator, now),
            )
    # IPC and Diamond always exist from the start.
    add_agent(conn, pid, "ipc", "ipc", state="active")
    add_agent(conn, pid, "diamond", "diamond", state="idle")
    return pid


def touch_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project_id))


def set_status(conn: sqlite3.Connection, project_id: str, status: str) -> None:
    conn.execute(
        "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
        (status, utcnow(), project_id),
    )


def set_flag(conn: sqlite3.Connection, project_id: str, flag: str) -> None:
    conn.execute(
        "UPDATE projects SET flag = ?, updated_at = ? WHERE id = ?", (flag, utcnow(), project_id)
    )


def set_wp_path(conn: sqlite3.Connection, project_id: str, wp_path: str) -> None:
    conn.execute(
        "UPDATE projects SET wp_path = ?, updated_at = ? WHERE id = ?",
        (wp_path, utcnow(), project_id),
    )


def delete_project(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------- hints ----------


def create_hint(conn: sqlite3.Connection, project_id: str, content: str, creator: str) -> Hint:
    from backend.blackboard.ids import next_hint_id

    now = utcnow()
    hid = next_hint_id(conn, project_id)
    conn.execute(
        "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
        (hid, project_id, content, creator, now),
    )
    return Hint(id=hid, content=content, creator=creator, created_at=now)


def list_hints(conn: sqlite3.Connection, project_id: str) -> list[Hint]:
    rows = conn.execute(
        "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at, rowid", (project_id,)
    ).fetchall()
    return [Hint(id=r["id"], content=r["content"], creator=r["creator"], created_at=r["created_at"]) for r in rows]


# ---------- agents + links (orchestration overlay) ----------


def add_agent(
    conn: sqlite3.Connection,
    project_id: str,
    name: str,
    role: str,
    state: str = "idle",
    start_fact_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO agents (project_id, name, role, state, start_fact_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, name, role, state, start_fact_id, utcnow()),
    )


def set_agent_state(conn: sqlite3.Connection, project_id: str, name: str, state: str) -> None:
    conn.execute(
        "UPDATE agents SET state = ? WHERE project_id = ? AND name = ?", (state, project_id, name)
    )


def list_agents(conn: sqlite3.Connection, project_id: str) -> list[Agent]:
    rows = conn.execute(
        "SELECT * FROM agents WHERE project_id = ? ORDER BY rowid", (project_id,)
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


def active_member_names(conn: sqlite3.Connection, project_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM agents WHERE project_id = ? AND role = 'member' AND state IN ('active','paused')",
        (project_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def add_link(conn: sqlite3.Connection, project_id: str, src: str, dst: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO agent_links (project_id, src, dst, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, src, dst, kind, utcnow()),
    )


def list_links(conn: sqlite3.Connection, project_id: str) -> list[AgentLink]:
    rows = conn.execute(
        "SELECT * FROM agent_links WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()
    return [
        AgentLink(id=r["id"], src=r["src"], dst=r["dst"], kind=r["kind"], created_at=r["created_at"])
        for r in rows
    ]


def link_exists(conn: sqlite3.Connection, project_id: str, src: str, dst: str, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_links WHERE project_id = ? AND src = ? AND dst = ? AND kind = ?",
        (project_id, src, dst, kind),
    ).fetchone()
    return row is not None


# ---------- reports ----------


def create_report(
    conn: sqlite3.Connection,
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
    conn.execute(
        "INSERT INTO reports (id, project_id, member, node_id, progress, difficulty, "
        "steps_json, directions_json, knowledge_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid,
            project_id,
            member,
            node_id,
            progress,
            difficulty,
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
        difficulty=difficulty,
        steps=steps,
        directions=directions,
        knowledge=knowledge,
        created_at=now,
    )


def list_reports(conn: sqlite3.Connection, project_id: str) -> list[Report]:
    rows = conn.execute(
        "SELECT * FROM reports WHERE project_id = ? ORDER BY created_at, rowid", (project_id,)
    ).fetchall()
    return [
        Report(
            id=r["id"],
            member=r["member"],
            node_id=r["node_id"],
            progress=r["progress"],
            difficulty=r["difficulty"],
            steps=json.loads(r["steps_json"]),
            directions=json.loads(r["directions_json"]),
            knowledge=json.loads(r["knowledge_json"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------- attachments ----------


def create_attachment(conn: sqlite3.Connection, project_id: str, filename: str, path: str) -> Attachment:
    now = utcnow()
    aid = next_attachment_id(conn, project_id)
    conn.execute(
        "INSERT INTO attachments (id, project_id, filename, path, created_at) VALUES (?, ?, ?, ?, ?)",
        (aid, project_id, filename, path, now),
    )
    return Attachment(id=aid, filename=filename, path=path, created_at=now)


def list_attachments(conn: sqlite3.Connection, project_id: str) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM attachments WHERE project_id = ? ORDER BY rowid", (project_id,)
    ).fetchall()
    return [
        Attachment(id=r["id"], filename=r["filename"], path=r["path"], created_at=r["created_at"])
        for r in rows
    ]


# ---------- broadcasts ----------


def add_broadcast(conn: sqlite3.Connection, project_id: str | None, title: str, flag: str) -> Broadcast:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO broadcasts (project_id, title, flag, created_at) VALUES (?, ?, ?, ?)",
        (project_id, title, flag, now),
    )
    return Broadcast(id=cur.lastrowid, project_id=project_id, title=title, flag=flag, created_at=now)


def list_broadcasts(conn: sqlite3.Connection, limit: int = 50) -> list[Broadcast]:
    rows = conn.execute(
        "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)
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


def project_detail(conn: sqlite3.Connection, project_id: str) -> ProjectDetail | None:
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


def project_summaries(conn: sqlite3.Connection) -> list[ProjectSummary]:
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
