from __future__ import annotations

from urllib.parse import quote, urlsplit

from backend.ops.models import PlatformWorkflowSpec
from backend.ops.service import _resolve_headers
from backend.platform.ret2shell import (
    Ret2ShellAdapter,
    Ret2ShellPreflightError,
    Ret2ShellRateLimitError,
)
from backend.platform.verdict import interpret_response


class CompetitionPlatform:
    """Freeze one Workflow revision; refresh only its referenced credentials."""
    def __init__(self, ops_service, workflow_id: str, spec: PlatformWorkflowSpec):
        self.ops = ops_service
        self.workflow_id = workflow_id
        self.spec = spec
        self.adapter = ops_service._adapter(workflow_id, spec)
        # Only ret2shell's client has the legacy game/instance API.  GZCTF
        # also owns a client, but its challenge-level contract is exposed by
        # the adapter and must not be sent through the ret2shell branches.
        self.client = self.adapter.client if isinstance(self.adapter, Ret2ShellAdapter) else None
        self._native = all(
            callable(getattr(self.adapter, name, None))
            for name in ("submit", "query")
        )

    @property
    def identity(self):
        adapter_identity = getattr(self.adapter, "identity", None)
        if adapter_identity:
            return str(adapter_identity)
        base = self.client.base_url if self.client else self.spec.challenges.list_url
        parsed = urlsplit(base)
        return f"{parsed.scheme}://{parsed.netloc}/{self.spec.competition_id}/{self.spec.team_id}"

    @property
    def supports_submit(self) -> bool:
        return self._native or self.client is not None or self.spec.submit is not None

    @property
    def supports_instances(self) -> bool:
        return self.client is not None

    def preflight(self):
        if not self.spec.competition_id or not self.spec.team_id:
            raise ValueError("Workflow needs explicit competition_id and team_id before Start")
        if callable(getattr(self.adapter, "preflight", None)):
            challenges = self.adapter.preflight()
        elif self.client:
            self.client.get_profile()
            self.client.get_game()
            challenges = self.adapter.fetch_challenges()
        elif not self.spec.submit or not self.spec.submit.success_path or not self.spec.submit.success_values:
            raise ValueError("Workflow needs explicit correct verdict mapping before Start")
        else:
            challenges = self.adapter.fetch_challenges()
        if self.spec.submit and self.spec.submit.pending_values and not self.spec.submit.query_url:
            raise ValueError("asynchronous judging requires a query_url")
        return challenges

    def challenges(self):
        native = getattr(self.adapter, "challenges", None)
        result = native() if callable(native) else self.adapter.fetch_challenges()
        if self.client:
            for challenge in result:
                challenge.remote = self.client.has_environment(int(challenge.external_id))
                challenge.solved = self.client.challenge_status(int(challenge.external_id)).get("solved") is True
        return result

    def submit(self, external_id: str, flag: str):
        if self._native:
            return self.adapter.submit(external_id, flag)
        if self.client:
            try:
                value = self.client.submit_flag(int(external_id), flag)
            except Ret2ShellRateLimitError:
                return {"verdict": "rate_limited", "retry_after": 300}
            except Ret2ShellPreflightError:
                return {"verdict": "unknown"}
            return self._r2s_verdict(value)
        return self.ops._submit(
            self.workflow_id, self.spec, external_id, flag,
            include_verdict=True,
        )

    @staticmethod
    def _r2s_verdict(value):
        return {"verdict": "correct" if value.get("solved") is True else "wrong" if value.get("solved") is False else "pending",
                "submission_id": str(value["id"]) if value.get("id") is not None else None}

    def query(self, external_id: str, submission_id: str):
        if self._native:
            return self.adapter.query(external_id, submission_id)
        if self.client:
            return self._r2s_verdict(self.client.get_submission(int(external_id), int(submission_id)))
        spec = self.spec.submit
        if not spec or not spec.query_url:
            return {"verdict": "unknown"}
        url = spec.query_url.replace("{{external_id}}", quote(external_id, safe="")).replace("{{submission_id}}", quote(submission_id, safe=""))
        secrets = self.ops.store.workflow_secrets(self.workflow_id)
        response = self.ops._http_client(self.spec).get(url, headers=_resolve_headers(spec.headers, secrets))
        result = interpret_response(response, spec)
        return {"verdict": result.status, "submission_id": submission_id, "retry_after": result.retry_after}

    def instances(self):
        method = getattr(self.adapter, "instances", None)
        if callable(method):
            return method()
        return self.client.list_instances() if self.client else []

    def start_instance(self, external_id: str):
        method = getattr(self.adapter, "start_instance", None)
        if callable(method):
            return method(external_id)
        if not self.client:
            raise ValueError("this platform has no verified instance lifecycle adapter")
        self.client.start_instance(int(external_id))
        return self.client.wait_for_instance(int(external_id), timeout=60)

    def stop_instance(self, external_id: str):
        method = getattr(self.adapter, "stop_instance", None)
        if callable(method):
            return method(external_id)
        if self.client:
            self.client.destroy_instance(int(external_id))

    def renew_instance(self, external_id: str):
        method = getattr(self.adapter, "renew_instance", None)
        if callable(method):
            return method(external_id)
        if self.client:
            self.client.renew_instance(int(external_id))

    def rebuild_instance(self, external_id: str):
        """Replace a failed remote instance when the platform supports it.

        Rebuild is deliberately an opt-in capability.  A generic HTTP or
        GZCTF workflow must never infer that deleting and recreating a target
        is safe; only an adapter/client that already owns the lifecycle gets
        this fallback.
        """
        method = getattr(self.adapter, "rebuild_instance", None)
        if callable(method):
            return method(external_id)
        if not self.client:
            raise ValueError("this platform has no verified instance rebuild lifecycle")
        self.client.destroy_instance(int(external_id))
        self.client.start_instance(int(external_id))
        return self.client.wait_for_instance(int(external_id), timeout=60)
