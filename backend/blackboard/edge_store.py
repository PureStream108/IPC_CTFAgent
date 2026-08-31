from __future__ import annotations

import re
import secrets
from difflib import SequenceMatcher
from typing import Any

from backend.blackboard.ids import next_intent_id, utcnow
from backend.blackboard.models import Intent


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^a-z0-9_./:-]+")


def normalize_intent_description(description: str) -> str:
    text = description.strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def intent_to_model(conn: Any, row: Any, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = %s AND project_id = %s ORDER BY fact_id",
        (row["id"], project_id),
    ).fetchall()
    return Intent(
        id=row["id"],
        **{"from": [source["fact_id"] for source in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        lease_token=row.get("lease_token"),
        lease_version=row.get("lease_version", 0),
        lease_expires_at=row.get("lease_expires_at"),
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def list_intents(conn: Any, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = %s ORDER BY created_at, id",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, row, project_id) for row in rows]


def get_intent(
    conn: Any,
    project_id: str,
    intent_id: str,
    *,
    for_update: bool = False,
):
    query = "SELECT * FROM intents WHERE id = %s AND project_id = %s"
    if for_update:
        query += " FOR UPDATE"
    return conn.execute(query, (intent_id, project_id)).fetchone()


def find_similar_open_intent(
    conn: Any,
    project_id: str,
    from_ids: list[str],
    description: str,
    *,
    threshold: float = 0.86,
) -> Intent | None:
    wanted_sources = set(from_ids)
    wanted = normalize_intent_description(description)
    if not wanted:
        return None
    for intent in list_intents(conn, project_id):
        if intent.to is not None or set(intent.from_) != wanted_sources:
            continue
        existing = normalize_intent_description(intent.description)
        if existing == wanted:
            return intent
        if existing and SequenceMatcher(None, existing, wanted).ratio() >= threshold:
            return intent
    return None


def create_intent(
    conn: Any,
    project_id: str,
    from_ids: list[str],
    description: str,
    creator: str,
    worker: str | None = None,
) -> Intent:
    now = utcnow()
    intent_id = next_intent_id(conn, project_id)
    conn.execute(
        """
        INSERT INTO intents (
            id, project_id, to_fact_id, description, creator, worker,
            last_heartbeat_at, created_at, concluded_at
        ) VALUES (%s, %s, NULL, %s, %s, NULL, NULL, %s, NULL)
        """,
        (intent_id, project_id, description, creator, now),
    )
    for fact_id in from_ids:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (%s, %s, %s)",
            (intent_id, project_id, fact_id),
        )
    if worker is not None:
        claim_intent(conn, project_id, intent_id, worker)
    row = get_intent(conn, project_id, intent_id)
    return intent_to_model(conn, row, project_id)


def complete_goal_intent(
    conn: Any,
    project_id: str,
    from_ids: list[str],
    description: str,
    creator: str,
    worker: str | None = None,
) -> Intent:
    """Create the single terminal goal edge for a project.

    The project row is the completion mutex.  Callers can safely race from
    multiple workers: the loser observes and reuses the already concluded goal
    intent instead of creating a second terminal edge.  A goal edge is a
    historical record, so it never retains a claim lease.
    """

    if not from_ids:
        raise ValueError("completion intent requires at least one source fact")
    project = conn.execute(
        "SELECT id, status FROM projects WHERE id = %s FOR UPDATE",
        (project_id,),
    ).fetchone()
    if project is None:
        raise ValueError("project not found")
    existing = conn.execute(
        """
        SELECT * FROM intents
        WHERE project_id = %s AND to_fact_id = 'goal'
        ORDER BY created_at, id
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if existing is not None:
        # Repair rows produced by older versions that concluded a goal while
        # leaving a worker lease behind.  This is safe under the project lock.
        conn.execute(
            """
            UPDATE intents
            SET lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL
            WHERE id = %s AND project_id = %s AND to_fact_id = 'goal'
            """,
            (existing["id"], project_id),
        )
        refreshed = get_intent(conn, project_id, existing["id"])
        return intent_to_model(conn, refreshed, project_id)
    if project["status"] == "solved":
        raise ValueError("project is solved without a completion edge")

    intent = create_intent(conn, project_id, from_ids, description, creator)
    now = utcnow()
    concluded = conn.execute(
        """
        UPDATE intents
        SET to_fact_id = 'goal', worker = %s, last_heartbeat_at = %s,
            concluded_at = %s, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL
        WHERE id = %s AND project_id = %s AND to_fact_id IS NULL
        RETURNING *
        """,
        (worker or creator, now, now, intent.id, project_id),
    ).fetchone()
    if concluded is None:
        # The project lock should make this unreachable, but fail closed if a
        # non-conforming caller changed the row inside the transaction.
        raise RuntimeError("completion intent could not be concluded")
    return intent_to_model(conn, concluded, project_id)


def claim_intent(
    conn: Any,
    project_id: str,
    intent_id: str,
    worker: str,
    *,
    lease_owner: str | None = None,
    lease_token: str | None = None,
    timeout: int | None = None,
) -> str | None:
    """Claim or renew an Intent and return its opaque fencing token."""

    owner = lease_owner or worker
    if timeout is None:
        settings = conn.execute("SELECT intent_timeout FROM settings WHERE id = 1").fetchone()
        timeout = int(settings["intent_timeout"] if settings else 30)
    now = utcnow()
    if lease_token:
        row = conn.execute(
            """
            UPDATE intents
            SET worker = %s,
                last_heartbeat_at = %s,
                lease_expires_at = %s::timestamptz + make_interval(secs => %s)
            WHERE id = %s AND project_id = %s AND concluded_at IS NULL
              AND lease_owner = %s AND lease_token = %s
              AND lease_expires_at IS NOT NULL AND lease_expires_at > now()
            RETURNING lease_token
            """,
            (worker, now, now, timeout, intent_id, project_id, owner, lease_token),
        ).fetchone()
        return row["lease_token"] if row else None

    token = secrets.token_urlsafe(24)
    row = conn.execute(
        """
        UPDATE intents
        SET worker = %s,
            lease_owner = %s,
            lease_token = %s,
            lease_version = lease_version + 1,
            last_heartbeat_at = %s,
            lease_expires_at = %s::timestamptz + make_interval(secs => %s)
        WHERE id = %s AND project_id = %s AND concluded_at IS NULL
          AND (
              lease_owner IS NULL
              OR (lease_expires_at IS NOT NULL AND lease_expires_at <= now())
          )
        RETURNING lease_token
        """,
        (worker, owner, token, now, now, timeout, intent_id, project_id),
    ).fetchone()
    return row["lease_token"] if row else None


def release_intent(
    conn: Any,
    project_id: str,
    intent_id: str,
    *,
    lease_owner: str | None = None,
    lease_token: str | None = None,
    worker: str | None = None,
) -> bool:
    query = """
        UPDATE intents
        SET worker = NULL, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_heartbeat_at = NULL
        WHERE id = %s AND project_id = %s AND concluded_at IS NULL
    """
    params: tuple[Any, ...] = (intent_id, project_id)
    if lease_owner is None and lease_token is None:
        if worker is None:
            query += " AND lease_owner IS NULL AND lease_token IS NULL"
        else:
            # Compatibility path for callers written before opaque tokens.
            # A worker still has to own a live lease; accepting an unclaimed
            # row here would let any caller release another worker's intent.
            query += " AND lease_owner = %s AND worker = %s " \
                "AND lease_expires_at IS NOT NULL AND lease_expires_at > now()"
            params += (worker, worker)
    elif lease_owner is None or lease_token is None:
        return False
    else:
        query += " AND lease_owner = %s AND lease_token = %s " \
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > now()"
        params += (lease_owner, lease_token)
    return conn.execute(query, params).rowcount == 1


def conclude_intent(
    conn: Any,
    project_id: str,
    intent_id: str,
    worker: str,
    fact_id: str,
    *,
    lease_owner: str | None = None,
    lease_token: str | None = None,
) -> bool:
    now = utcnow()
    query = """
        UPDATE intents
        SET to_fact_id = %s, worker = %s, last_heartbeat_at = %s,
            concluded_at = %s, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL
        WHERE id = %s AND project_id = %s AND concluded_at IS NULL
    """
    params: tuple[Any, ...] = (fact_id, worker, now, now, intent_id, project_id)
    if lease_owner is None and lease_token is None:
        # A legacy conclude call can still be accepted when the worker owns a
        # live lease.  An unclaimed intent must never be concluded without a
        # fencing owner, otherwise a stale/untrusted caller can write facts.
        query += " AND lease_owner = %s AND worker = %s " \
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > now()"
        params += (worker, worker)
    elif lease_owner is None or lease_token is None:
        return False
    else:
        query += " AND lease_owner = %s AND lease_token = %s " \
            "AND lease_expires_at IS NOT NULL AND lease_expires_at > now()"
        params += (lease_owner, lease_token)
    return conn.execute(query, params).rowcount == 1


def expire_workers(conn: Any, timeout: int, project_id: str | None = None) -> None:
    timeout = max(1, int(timeout))
    query = """
        UPDATE intents
        SET worker = NULL, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_heartbeat_at = NULL,
            retry_count = retry_count + 1
        WHERE to_fact_id IS NULL AND lease_owner IS NOT NULL
          AND (
              (lease_expires_at IS NOT NULL AND lease_expires_at <= now())
              OR (
                  last_heartbeat_at IS NOT NULL
                  AND last_heartbeat_at < now() - make_interval(secs => %s)
              )
          )
    """
    params: tuple[Any, ...] = (timeout,)
    if project_id is not None:
        query += " AND project_id = %s"
        params += (project_id,)
    conn.execute(query, params)
