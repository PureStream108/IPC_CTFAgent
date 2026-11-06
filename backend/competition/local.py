from __future__ import annotations

from typing import Any

from backend.platform.mapping import PlatformChallenge


class LocalPlatform:
    """Adapter-shaped view of one already-imported local challenge.

    A local run owns exactly one Project that the operator created through the
    normal Project API.  Nothing is fetched from a network, nothing is
    provisioned, and flag verdicts come from the same durable local evidence
    path used before runs existed.
    """

    def __init__(self, project: dict[str, Any], db) -> None:
        self.project = dict(project or {})
        self.db = db

    @property
    def external_id(self) -> str:
        return str(self.project.get("project_id") or "")

    @property
    def identity(self) -> str:
        return f"local://{self.external_id}"

    @property
    def supports_submit(self) -> bool:
        return True

    @property
    def supports_instances(self) -> bool:
        return False

    def instance_limit(self) -> None:
        return None

    def network_diagnostics(self) -> dict:
        return {"reachable": True, "source": "local"}

    def preflight(self) -> list[PlatformChallenge]:
        return self.challenges()

    def challenges(self) -> list[PlatformChallenge]:
        if not self.external_id:
            return []
        return [
            PlatformChallenge(
                external_id=self.external_id,
                title=str(self.project.get("title") or self.external_id),
                category=str(self.project.get("category") or ""),
                description=str(self.project.get("goal") or ""),
                remote=False,
                solved=bool(self.project.get("solved")),
            )
        ]

    def submit(self, external_id: str, flag: str) -> dict:
        from backend.core.ipc import submit_flag_candidate

        if str(external_id) != self.external_id:
            return {"verdict": "rejected", "message": "unknown local challenge"}
        try:
            with self.db.connect() as connection:
                result = submit_flag_candidate(
                    connection, self.external_id, flag, source="competition"
                )
        except ValueError as exc:
            return {"verdict": "wrong", "message": str(exc)}
        return {"verdict": "correct", "submission_id": str(result.get("flag") or "")}

    def query(self, external_id: str, submission_id: str) -> dict:
        return {"verdict": "unknown", "submission_id": submission_id}

    def instances(self) -> list[dict]:
        return []

    def start_instance(self, external_id: str) -> dict:
        raise RuntimeError("local challenges do not provision instances")

    def renew_instance(self, external_id: str) -> dict:
        raise RuntimeError("local challenges do not provision instances")

    def rebuild_instance(self, external_id: str) -> dict:
        raise RuntimeError("local challenges do not provision instances")

    def stop_instance(self, external_id: str) -> dict:
        return {"external_id": external_id, "state": "stopped"}
