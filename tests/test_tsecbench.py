from __future__ import annotations

import json

import pytest

from backend.platform.factory import build_adapter
from backend.platform.mapping import FieldMapping
from backend.platform.tsecbench import (
    TSECBenchAdapter,
    TSECBenchAuthError,
    TSECBenchClient,
    TSECBenchError,
    TSECBenchInstanceError,
    TSECBenchPreflightError,
)


class _Response:
    def __init__(self, payload=None, status_code: int = 200, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)

    def close(self):
        return None


def _error_payload(code: str, message: str = "") -> dict:
    return {"code": code, "message": message or code, "detail": {}}


class _FakeClient:
    """Duck-typed client double mirroring TSECBenchClient's surface."""

    base_url = "https://tsecbench.zc.tencent.com"

    def __init__(
        self,
        challenges: list[dict] | None = None,
        *,
        start_result=None,
        submit_result=None,
        close_error: Exception | None = None,
    ):
        self.challenges_payload = challenges or []
        self.start_result = start_result if start_result is not None else {"container_addr": ["10.0.0.8:8080"]}
        self.submit_result = submit_result if submit_result is not None else {"correct": True, "awarded": 300, "cumulative_score": 300, "correct_flag_count": 1, "total_flag_count": 1, "matched_flag_index": 0}
        self.close_error = close_error
        self.start_calls: list[str] = []
        self.submit_calls: list[tuple[str, str]] = []

    def list_challenges(self):
        return list(self.challenges_payload)

    def start_container(self, unique_code):
        self.start_calls.append(unique_code)
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def close_container(self, unique_code):
        if self.close_error is not None:
            raise self.close_error
        return {"unique_code": unique_code, "closed": True}

    def submit_flag(self, unique_code, flag):
        self.submit_calls.append((unique_code, flag))
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    def hint(self, unique_code):
        return {"unique_code": unique_code, "hint": "try harder"}


def _challenge(code: str, description: str = "", **extra) -> dict:
    base = {
        "unique_code": code,
        "description": description,
        "difficulty": "easy",
        "level": 1,
        "total_score": 300,
        "flag_count": 1,
        "correct_flag_count": 0,
        "is_completed": False,
        "container_status": "stopped",
        "container_addr": [],
    }
    base.update(extra)
    return base


def _mapping(**extra) -> FieldMapping:
    return FieldMapping(
        platform="tsecbench",
        list_url="https://tsecbench.zc.tencent.com",
        **extra,
    )


def test_client_sends_token_header_and_parses_the_challenge_list():
    session = _Session([_Response([_challenge("g-01")])])
    client = TSECBenchClient("https://tsecbench.zc.tencent.com/", "tok-1", session=session)
    items = client.list_challenges()
    assert items[0]["unique_code"] == "g-01"
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://tsecbench.zc.tencent.com/openapi/v1/challenges"
    assert kwargs["headers"]["BENCHMARK_TOKEN"] == "tok-1"


def test_client_submit_posts_unique_code_and_flag():
    session = _Session([_Response({"correct": False, "awarded": 0})])
    client = TSECBenchClient("https://tsecbench.zc.tencent.com", "tok", session=session)
    client.submit_flag("g-05", "HTB{flag}")
    _, url, kwargs = session.calls[0]
    assert url.endswith("/openapi/v1/challenges/submit")
    assert kwargs["json"] == {"unique_code": "g-05", "flag": "HTB{flag}"}


@pytest.mark.parametrize(
    "status,code,message,reason",
    [
        (404, "task_not_found", "token invalid", "auth"),
        (404, "challenge_not_found", "missing", "missing"),
        (409, "duplicate", "already accepted", "duplicate"),
        (409, "invalid_state", "当前活跃的题目实例数已达到上限", "quota"),
        (409, "invalid_state", "任务已结束", "ended"),
        (503, "resource_unavailable", "exhausted", "transient"),
        (500, "internal_error", "boom", "transient"),
        (422, "", "bad body", "rejected"),
    ],
)
def test_client_error_classification(status, code, message, reason):
    session = _Session([_Response(_error_payload(code, message), status_code=status)])
    client = TSECBenchClient("https://tsecbench.zc.tencent.com", "tok", session=session)
    with pytest.raises(TSECBenchError) as raised:
        client.list_challenges()
    assert raised.value.reason == reason
    if reason == "auth":
        assert isinstance(raised.value, TSECBenchAuthError)


def test_client_requires_base_url_and_token():
    with pytest.raises(TSECBenchPreflightError):
        TSECBenchClient("", "tok", session=_Session([])).list_challenges()
    with pytest.raises(TSECBenchAuthError):
        TSECBenchClient("https://tsecbench.zc.tencent.com", "", session=_Session([])).list_challenges()


def test_adapter_normalises_challenges_and_derives_categories():
    client = _FakeClient([
        _challenge("g-05", "RSA 加密脚本 source.py 与输出 output.txt", total_score=300),
        _challenge("g-02", "pickle 服务逃逸沙箱，拿到代码执行", difficulty="hard", total_score=700),
        _challenge("g-34", "老网站版本库泄露，藏在历史里"),
        _challenge("x-99", "完全看不懂的东西"),
    ])
    adapter = TSECBenchAdapter(client, _mapping())
    challenges = {item.external_id: item for item in adapter.fetch_challenges()}
    assert challenges["g-05"].category == "crypto"
    assert challenges["g-02"].category == "pwn"
    assert challenges["g-34"].category == "web"
    assert challenges["x-99"].category == "misc"
    assert challenges["g-05"].remote is True
    assert challenges["g-05"].title == "g-05"
    assert challenges["g-05"].platform_data["score"] == 300
    assert challenges["g-05"].attachment_urls == []


def test_adapter_category_map_keywords_override_the_builtin_table():
    client = _FakeClient([_challenge("g-05", "RSA 加密脚本")])
    adapter = TSECBenchAdapter(client, _mapping(category_map={"rsa": "reverse"}))
    challenge = adapter.fetch_challenges()[0]
    assert challenge.category == "reverse"


def test_adapter_preflight_fails_loudly_on_an_empty_task():
    adapter = TSECBenchAdapter(_FakeClient([]), _mapping())
    with pytest.raises(TSECBenchPreflightError):
        adapter.preflight()


def test_adapter_instance_lifecycle_start_reads_and_renews():
    client = _FakeClient([
        _challenge("g-01", container_status="available", container_addr=["10.0.0.8:8080"]),
        _challenge("g-02"),
    ])
    adapter = TSECBenchAdapter(client, _mapping())
    assert adapter.supports_instances is True
    assert adapter.instance_limit() == 3

    view = adapter.start_instance("g-01")
    assert view["state"] == "ready"
    assert view["entry"] == "10.0.0.8:8080"
    assert client.start_calls == ["g-01"]

    live = adapter.instances()
    assert [item["external_id"] for item in live] == ["g-01"]

    renewed = adapter.renew_instance("g-01")
    assert renewed["state"] == "ready"
    assert renewed["addresses"] == ["10.0.0.8:8080"]


def test_adapter_start_without_an_address_is_a_retryable_failure():
    client = _FakeClient([_challenge("g-01")], start_result={"container_addr": []})
    adapter = TSECBenchAdapter(client, _mapping())
    with pytest.raises(TSECBenchInstanceError) as raised:
        adapter.start_instance("g-01")
    assert raised.value.reason == "transient"


def test_adapter_renew_reports_a_stopped_container_as_expired():
    client = _FakeClient([_challenge("g-01", container_status="stopped")])
    adapter = TSECBenchAdapter(client, _mapping())
    with pytest.raises(TSECBenchInstanceError) as raised:
        adapter.renew_instance("g-01")
    assert raised.value.reason == "expired"


def test_adapter_stop_swallows_a_missing_challenge_but_raises_others():
    missing = _FakeClient([], close_error=TSECBenchError("gone", reason="missing"))
    TSECBenchAdapter(missing, _mapping()).stop_instance("g-01")

    busy = _FakeClient([], close_error=TSECBenchError("busy", reason="transient"))
    with pytest.raises(TSECBenchInstanceError):
        TSECBenchAdapter(busy, _mapping()).stop_instance("g-01")


@pytest.mark.parametrize(
    "outcome,verdict,extra",
    [
        ({"correct": True, "awarded": 300, "cumulative_score": 300, "correct_flag_count": 1, "total_flag_count": 1, "matched_flag_index": 0}, "correct", {"awarded": 300}),
        ({"correct": False, "awarded": 0}, "wrong", {}),
        (TSECBenchError("dup", reason="duplicate"), "correct", {}),
        (TSECBenchAuthError("bad token"), "auth_required", {}),
        (TSECBenchError("flaky", reason="transient"), "unknown", {}),
        (TSECBenchError("gone", reason="missing"), "rejected", {}),
        (TSECBenchError("ended", reason="ended"), "rejected", {}),
    ],
)
def test_adapter_submit_maps_platform_results_to_verdicts(outcome, verdict, extra):
    client = _FakeClient([_challenge("g-01")], submit_result=outcome)
    adapter = TSECBenchAdapter(client, _mapping())
    result = adapter.submit("g-01", "HTB{flag}")
    assert result["verdict"] == verdict
    for key, value in extra.items():
        assert result[key] == value
    assert client.submit_calls == [("g-01", "HTB{flag}")]


def test_adapter_download_attachments_is_empty_by_design(tmp_path):
    adapter = TSECBenchAdapter(_FakeClient([_challenge("g-01")]), _mapping())
    challenge = adapter.fetch_challenges()[0]
    assert adapter.download_attachments(challenge, tmp_path) == []


def test_factory_builds_a_tsecbench_adapter_with_the_benchmark_token():
    adapter = build_adapter(
        FieldMapping(
            platform="tsecbench",
            list_url="https://tsecbench.zc.tencent.com/openapi/v1/challenges",
        ),
        credentials={"benchmark_token": "tok-2"},
    )
    assert isinstance(adapter, TSECBenchAdapter)
    assert adapter.client.base_url == "https://tsecbench.zc.tencent.com"
    assert adapter.client.token == "tok-2"


def test_tsecbench_workflow_preset_builds_a_template_spec():
    from backend.ops.presets import TSECBenchWorkflowInput, workflow_templates

    templates = {item["id"]: item for item in workflow_templates()}
    assert "tsecbench" in templates
    assert templates["tsecbench"]["secret_names"] == ["benchmark_token"]

    settings = TSECBenchWorkflowInput(
        base_url="https://tsecbench.zc.tencent.com",
        name="",
        secrets={"benchmark_token": "tok"},
    )
    spec = settings.workflow_spec()
    assert spec.template_id == "tsecbench"
    assert spec.challenges.platform == "tsecbench"
    assert spec.challenges.list_url == "https://tsecbench.zc.tencent.com"
    assert spec.team_id == "benchmark"
    assert "benchmark_token" in spec.allowed_secret_names()
    assert "benchmark_token" not in spec.required_secret_names()
    assert spec.challenges.game_id is None


def test_tsecbench_workflow_preset_rejects_foreign_credentials():
    from backend.ops.presets import TSECBenchWorkflowInput

    with pytest.raises(Exception):
        TSECBenchWorkflowInput(
            base_url="https://tsecbench.zc.tencent.com",
            secrets={"gzctf_token": "nope"},
        )
    with pytest.raises(Exception):
        TSECBenchWorkflowInput(base_url="https://tsecbench.zc.tencent.com/openapi", secrets={})
