from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from backend.competition.store import CompetitionConflict


class SharedWorkspace:
    def __init__(
        self,
        db,
        root: Path,
        *,
        max_bytes: int = 100 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
        min_free_bytes: int = 64 * 1024 * 1024,
    ):
        self.db = db
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes

    @staticmethod
    def _relative(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("artifact path must be a normalized relative path")
        normalized = path.as_posix()
        if len(normalized) > 500:
            raise ValueError("artifact path is too long")
        return normalized

    def _target(self, challenge_id: str, relative: str) -> Path:
        base = (self.root / challenge_id / "shared").resolve()
        target = (base / relative).resolve()
        if target == base or base not in target.parents:
            raise ValueError("artifact path escapes the challenge workspace")
        return target

    def read(self, challenge_id: str, path: str) -> tuple[dict, bytes]:
        relative = self._relative(path)
        target = self._target(challenge_id, relative)
        with self.db.connect() as connection:
            row = connection.execute(
                """SELECT * FROM competition_artifacts
                   WHERE challenge_id=%s AND path=%s""",
                (challenge_id, relative),
            ).fetchone()
        if row is None:
            raise KeyError(relative)
        try:
            content = target.read_bytes()
        except FileNotFoundError as exc:
            raise CompetitionConflict("artifact metadata exists but the file is missing") from exc
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise CompetitionConflict("artifact file does not match its persisted version")
        return row, content

    def write(
        self,
        challenge_id: str,
        path: str,
        content: bytes,
        *,
        session_id: str,
        assignment_id: str,
        lease_owner: str,
        lease_epoch: int,
        expected_version: int | None,
    ) -> dict:
        relative = self._relative(path)
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("artifact content exceeds the configured byte limit")
        target = self._target(challenge_id, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        temporary: Path | None = None
        previous: bytes | None = None
        write_started = False
        try:
            with self.db.connect() as connection:
                # Serialize quota calculations across different files in the
                # same challenge, then take the narrower per-path lock.
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"artifact-quota:{challenge_id}",),
                )
                connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"artifact:{challenge_id}:{relative}",))
                session = connection.execute(
                    """SELECT 1 FROM competition_assignments
                       WHERE id=%s AND session_id=%s AND challenge_id=%s
                       AND lease_owner=%s AND epoch=%s AND released_at IS NULL
                       AND lease_expires_at>now()""",
                    (
                        assignment_id, session_id, challenge_id,
                        lease_owner, lease_epoch,
                    ),
                ).fetchone()
                if session is None:
                    raise CompetitionConflict("assignment lease is stale or does not own this session")
                current = connection.execute(
                    """SELECT * FROM competition_artifacts
                       WHERE challenge_id=%s AND path=%s FOR UPDATE""",
                    (challenge_id, relative),
                ).fetchone()
                actual = current["version"] if current else None
                if actual != expected_version:
                    raise CompetitionConflict(
                        f"artifact version conflict: expected {expected_version}, found {actual}"
                    )
                total = connection.execute(
                    """SELECT COALESCE(sum(size_bytes),0) AS value
                       FROM competition_artifacts WHERE challenge_id=%s""",
                    (challenge_id,),
                ).fetchone()["value"]
                projected = total - (current["size_bytes"] if current else 0) + len(content)
                if projected > self.max_total_bytes:
                    raise CompetitionConflict("challenge artifact quota is exhausted")
                free = shutil.disk_usage(target.parent).free
                if free - len(content) < self.min_free_bytes:
                    raise CompetitionConflict("insufficient disk space for an atomic artifact write")
                previous = target.read_bytes() if target.is_file() else None
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=f".{target.name}.",
                    suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                write_started = True
                temporary = None
                if current:
                    row = connection.execute(
                        """UPDATE competition_artifacts SET version=version+1,sha256=%s,
                           size_bytes=%s,updated_by_session=%s,updated_at=now()
                           WHERE challenge_id=%s AND path=%s RETURNING *""",
                        (digest, len(content), session_id, challenge_id, relative),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """INSERT INTO competition_artifacts
                           (challenge_id,path,sha256,size_bytes,updated_by_session)
                           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                        (challenge_id, relative, digest, len(content), session_id),
                    ).fetchone()
                return row
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if write_started:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=target.parent, prefix=f".{target.name}.",
                        suffix=".restore", delete=False,
                    ) as handle:
                        restore = Path(handle.name)
                        handle.write(previous)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(restore, target)
            raise
