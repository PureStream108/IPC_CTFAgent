"""Database-backed fencing between the legacy and competition engines."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


ENGINE_KINDS = {"legacy", "competition"}


def _validate(kind: str, owner: str, seconds: int) -> None:
    if kind not in ENGINE_KINDS:
        raise ValueError("unknown engine kind")
    if not owner or len(owner) > 200:
        raise ValueError("engine owner is required")
    if seconds < 5 or seconds > 300:
        raise ValueError("engine lease must be between 5 and 300 seconds")


def claim_engine_lease(
    db,
    project_id: str,
    kind: str,
    owner: str,
    *,
    seconds: int = 60,
) -> dict[str, Any] | None:
    """Claim or renew a project scheduler lease atomically.

    ``None`` means another engine has a live lease.  Renewing the exact same
    kind/owner keeps the epoch; taking over an expired lease increments it.
    """

    _validate(kind, owner, seconds)
    with db.connect() as connection:
        row = connection.execute(
            """SELECT id,engine_kind,engine_owner,engine_epoch,engine_lease_expires_at
               FROM projects WHERE id=%s FOR UPDATE""",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        same_owner = row["engine_kind"] == kind and row["engine_owner"] == owner
        live = bool(connection.execute(
            "SELECT 1 FROM projects WHERE id=%s AND engine_lease_expires_at>now()",
            (project_id,),
        ).fetchone())
        if live and not same_owner:
            return None
        # A live lease may be renewed by the same owner without fencing its
        # in-flight work.  Once the lease has expired, even that same owner
        # must receive a new epoch so a delayed worker cannot keep writing
        # with the previous fencing token.
        keep_epoch = live and same_owner
        return connection.execute(
            """UPDATE projects SET engine_kind=%s,engine_owner=%s,
                   engine_epoch=CASE WHEN %s THEN engine_epoch ELSE engine_epoch+1 END,
                   engine_lease_expires_at=now()+(%s*interval '1 second'),
                   updated_at=now()
               WHERE id=%s
               RETURNING id,engine_kind,engine_owner,engine_epoch,engine_lease_expires_at""",
            (kind, owner, keep_epoch, seconds, project_id),
        ).fetchone()


def renew_engine_lease(
    db,
    project_id: str,
    kind: str,
    owner: str,
    *,
    epoch: int,
    seconds: int = 60,
) -> bool:
    """Renew only an unexpired lease owned by the exact fencing tuple."""

    _validate(kind, owner, seconds)
    if not isinstance(epoch, int) or epoch < 1:
        raise ValueError("engine lease epoch is required")
    with db.connect() as connection:
        return bool(connection.execute(
            """UPDATE projects SET engine_lease_expires_at=now()+(%s*interval '1 second'),
               updated_at=now()
               WHERE id=%s AND engine_kind=%s AND engine_owner=%s
                 AND engine_epoch=%s
                 AND engine_lease_expires_at>now()
               RETURNING id""",
            (seconds, project_id, kind, owner, epoch),
        ).fetchone())


def release_engine_lease(
    db, project_id: str, kind: str, owner: str, *, epoch: int
) -> bool:
    """Clear a lease only when the caller still owns its fencing tuple."""

    _validate(kind, owner, 60)
    if not isinstance(epoch, int) or epoch < 1:
        raise ValueError("engine lease epoch is required")
    with db.connect() as connection:
        return bool(connection.execute(
            """UPDATE projects SET engine_kind=NULL,engine_owner=NULL,
               engine_lease_expires_at=NULL,updated_at=now()
               WHERE id=%s AND engine_kind=%s AND engine_owner=%s
                 AND engine_epoch=%s
               RETURNING id""",
            (project_id, kind, owner, epoch),
        ).fetchone())


def release_engine_leases_for_owner(
    db, kind: str, owner: str, *, epochs: Mapping[str, int]
) -> int:
    """Release only the explicitly fenced project leases held by an engine.

    A bulk owner-only update is unsafe after a restart because the same owner
    string may already hold a newer epoch.  Callers must provide the epochs
    they actually acquired.
    """

    _validate(kind, owner, 60)
    if not isinstance(epochs, Mapping):
        raise ValueError("engine lease epochs are required")
    released = 0
    for project_id, epoch in epochs.items():
        if not isinstance(epoch, int) or epoch < 1:
            raise ValueError("engine lease epoch is required")
        if release_engine_lease(db, project_id, kind, owner, epoch=epoch):
            released += 1
    return released


def engine_lease(db, project_id: str) -> dict[str, Any] | None:
    """Return the current scheduler lease for diagnostics."""

    with db.connect() as connection:
        return connection.execute(
            """SELECT id,engine_kind,engine_owner,engine_epoch,engine_lease_expires_at
               FROM projects WHERE id=%s""",
            (project_id,),
        ).fetchone()
