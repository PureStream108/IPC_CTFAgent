from __future__ import annotations

from copy import deepcopy

from backend.platform.mapping import PlatformChallenge


class FixturePlatform:
    """Deterministic platform contract used by acceptance and fault tests."""

    def __init__(
        self,
        challenges: list[PlatformChallenge],
        *,
        flags: dict[str, str] | None = None,
        remote_limit: int = 0,
        renew_failures: int = 0,
        rebuild_on_renew_failure: bool = False,
    ) -> None:
        self._challenges = {item.external_id: item.model_copy(deep=True) for item in challenges}
        self.flags = dict(flags or {})
        self.remote_limit = remote_limit
        self._instances: dict[str, dict] = {}
        self._submissions: dict[str, dict] = {}
        self._next_submission = 1
        self.calls: list[tuple[str, str]] = []
        self.query_calls: list[tuple[str, str]] = []
        self.renew_calls: list[str] = []
        self.renew_failures = max(0, int(renew_failures))
        self.rebuild_on_renew_failure = bool(rebuild_on_renew_failure)
        self.rebuilds: list[str] = []

    @property
    def identity(self) -> str:
        return "fixture://competition/team"

    @property
    def supports_submit(self) -> bool:
        """Expose the same explicit write capability as production adapters."""
        return True

    def preflight(self) -> list[PlatformChallenge]:
        return self.challenges()

    def challenges(self) -> list[PlatformChallenge]:
        return [item.model_copy(deep=True) for item in self._challenges.values()]

    def replace_challenges(self, challenges: list[PlatformChallenge]) -> None:
        self._challenges = {item.external_id: item.model_copy(deep=True) for item in challenges}

    def submit(self, external_id: str, flag: str) -> dict:
        if external_id not in self._challenges:
            return {"verdict": "rejected"}
        self.calls.append((external_id, flag))
        if flag == "fixture:rate-limited":
            return {"verdict": "rate_limited", "retry_after": 60}
        submission_id = str(self._next_submission)
        self._next_submission += 1
        if flag.startswith("fixture:pending:"):
            verdict = flag.removeprefix("fixture:pending:")
            self._submissions[submission_id] = {
                "verdict": verdict if verdict in {"correct", "wrong"} else "unknown",
                "submission_id": submission_id,
            }
            return {"verdict": "pending", "submission_id": submission_id}
        verdict = "correct" if self.flags.get(external_id) == flag else "wrong"
        return {"verdict": verdict, "submission_id": submission_id}

    def query(self, external_id: str, submission_id: str) -> dict:
        self.query_calls.append((external_id, submission_id))
        return deepcopy(
            self._submissions.get(
                submission_id,
                {"verdict": "unknown", "submission_id": submission_id},
            )
        )

    def instances(self) -> list[dict]:
        return [deepcopy(value) for value in self._instances.values()]

    def start_instance(self, external_id: str) -> dict:
        if external_id not in self._challenges:
            raise KeyError(external_id)
        if external_id in self._instances:
            return deepcopy(self._instances[external_id])
        if len(self._instances) >= self.remote_limit:
            raise RuntimeError("fixture instance quota exhausted")
        instance = {
            "external_id": external_id,
            "state": "ready",
            "generation": 1,
            "renewals": 0,
        }
        self._instances[external_id] = instance
        return deepcopy(instance)

    def renew_instance(self, external_id: str) -> dict:
        instance = self._instances.get(external_id)
        if instance is None:
            raise KeyError(external_id)
        self.renew_calls.append(external_id)
        if self.renew_failures:
            self.renew_failures -= 1
            raise RuntimeError("fixture renewal failure")
        instance["renewals"] += 1
        return {**instance, "renew_after": 300}

    def rebuild_instance(self, external_id: str) -> dict:
        if not self.rebuild_on_renew_failure:
            raise RuntimeError("fixture rebuild is disabled")
        previous = self._instances.get(external_id)
        if previous is None:
            raise KeyError(external_id)
        generation = int(previous.get("generation", 0)) + 1
        replacement = {
            "external_id": external_id,
            "state": "ready",
            "generation": generation,
            "renewals": 0,
            "renew_after": 300,
        }
        self._instances[external_id] = replacement
        self.rebuilds.append(external_id)
        return deepcopy(replacement)

    def stop_instance(self, external_id: str) -> None:
        self._instances.pop(external_id, None)
