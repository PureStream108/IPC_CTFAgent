from __future__ import annotations

from urllib.parse import quote, urlsplit

from backend.ops.models import PlatformWorkflowSpec
from backend.ops.service import _resolve_headers
from backend.platform.adapter import PlatformAdapter
from backend.platform.verdict import interpret_response


class CompetitionPlatform:
    """Freeze one Workflow revision; refresh only its referenced credentials."""
    def __init__(self, ops_service, workflow_id: str, spec: PlatformWorkflowSpec):
        self.ops = ops_service
        self.workflow_id = workflow_id
        self.spec = spec
        self.adapter = ops_service._adapter(workflow_id, spec)

    @property
    def identity(self):
        adapter_identity = getattr(self.adapter, "identity", None)
        if adapter_identity:
            base = str(adapter_identity).rstrip("/")
            team = quote(str(self.spec.team_id), safe="")
            team_marker = f"/team/{team}"
            if base.endswith(f"/{team}") or base.endswith(team_marker) or (
                team_marker in base and "/account/" in base
            ):
                return base
            return f"{base}/{quote(str(self.spec.competition_id), safe='')}/{team}"
        base = self.spec.challenges.list_url
        parsed = urlsplit(base)
        return (
            f"{parsed.scheme}://{parsed.netloc}/"
            f"{quote(str(self.spec.competition_id), safe='')}/"
            f"{quote(str(self.spec.team_id), safe='')}"
        )

    @property
    def supports_submit(self) -> bool:
        declared = getattr(self.adapter, "supports_submit", None)
        return bool(declared() if callable(declared) else declared) or self.spec.submit is not None

    @property
    def supports_instances(self) -> bool:
        declared = getattr(self.adapter, "supports_instances", False)
        return bool(declared() if callable(declared) else declared)

    def preflight(self):
        if not self.spec.competition_id or not self.spec.team_id:
            raise ValueError("Workflow needs explicit competition_id and team_id before Start")
        adapter_preflight = getattr(type(self.adapter), "preflight", None)
        if (
            callable(getattr(self.adapter, "preflight", None))
            and adapter_preflight is not PlatformAdapter.preflight
        ):
            challenges = self.adapter.preflight()
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
        return result

    def submit(self, external_id: str, flag: str):
        native = getattr(self.adapter, "submit", None)
        if callable(native):
            return native(external_id, flag)
        return self.ops._submit(
            self.workflow_id, self.spec, external_id, flag,
            include_verdict=True,
        )

    def query(self, external_id: str, submission_id: str):
        native = getattr(self.adapter, "query", None)
        if callable(native):
            return native(external_id, submission_id)
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
        return []

    def start_instance(self, external_id: str):
        method = getattr(self.adapter, "start_instance", None)
        if callable(method):
            return method(external_id)
        raise ValueError("this platform has no verified instance lifecycle adapter")

    def stop_instance(self, external_id: str):
        method = getattr(self.adapter, "stop_instance", None)
        if callable(method):
            return method(external_id)
        raise ValueError("this platform has no verified instance lifecycle adapter")

    def renew_instance(self, external_id: str):
        method = getattr(self.adapter, "renew_instance", None)
        if callable(method):
            return method(external_id)
        raise ValueError("this platform has no verified instance lifecycle adapter")

    def rebuild_instance(self, external_id: str):
        """Replace a failed remote instance when the platform supports it.

        Rebuild is deliberately an opt-in capability.  A workflow must never
        infer that deleting and recreating a target is safe; only an adapter
        that explicitly owns the lifecycle gets this operation.
        """
        method = getattr(self.adapter, "rebuild_instance", None)
        if callable(method):
            return method(external_id)
        raise ValueError("this platform has no verified instance rebuild lifecycle")
