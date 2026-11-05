from __future__ import annotations

import pytest

from backend.platform.ichunqiu import (
    IchunqiuAdapter,
    IchunqiuClient,
    IchunqiuError,
    IchunqiuLoginError,
    sign_payload,
)
from backend.platform.ichunqiu.workflow import IchunqiuWorkflowInput
from backend.platform.mapping import FieldMapping

BASE = "https://match.ichunqiu.com"


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        data = self.payload
        if callable(data):
            data = data(url, json)
        return _Response(data)


def test_sign_matches_a_real_browser_request():
    """Golden vector captured from match.ichunqiu.com on 2026-10-31."""

    payload = {
        "url_key": "2026qwbs10",
        "is_encrypt": 2,
        "k": "",
        "stamp": 1793417129404,
        "token": "",
        "rs": "ef76d51ecc6ab5460da844d1b88e518d",
    }
    assert sign_payload(payload) == "66c96e6842e7bf4a08cb66b00c90322dc71df153"


def test_client_sends_the_signed_envelope_and_resolves_the_match_key():
    session = _Session(
        {"code": 0, "data": {"title": "第十届强网杯", "url_key": "opaque-key", "team_docker_limit": 2}}
    )
    client = IchunqiuClient(base_url=BASE, url_key="2026qwbs10", token="tok")
    client.session = session

    match = client.resolve_match()

    assert match["url_key"] == "opaque-key"
    assert client.key == "opaque-key"
    call = session.calls[0]
    assert call["url"] == "https://apiterminator.ichunqiu.com/match/detail"
    assert call["json"]["url_key"] == "2026qwbs10"
    assert call["json"]["is_encrypt"] == 2
    assert call["headers"]["SIGN"] == sign_payload(call["json"])


def test_login_errors_are_classified_from_the_envelope():
    session = _Session({"code": 114, "message": "请登录平台"})
    client = IchunqiuClient(base_url=BASE, url_key="2026qwbs10", token="bad")
    client.session = session
    with pytest.raises(IchunqiuLoginError):
        client.categories()


class _FakeClient:
    base_url = BASE
    slug = "2026qwbs10"
    key = "opaque-key"
    token = "tok-123"
    timeout = 5
    logged_in = True

    def __init__(self):
        self.match = {"team_docker_limit": 2, "user_docker_limit": 1}
        self.submitted = []
        self.fail_submit = None

    def resolve_match(self):
        return self.match

    def categories(self):
        return [
            {
                "id": 4796,
                "title": "Misc",
                "question": {
                    "lists": [
                        {"id": "KVlvgNuOwJXPAyAQ", "title": "宇宙微波背景辐射", "type_id": 1, "red_score": 100}
                    ]
                },
            },
            {
                "id": 4799,
                "title": "PWN",
                "question": {"lists": [{"id": "easytl", "title": "easytl", "type_id": 2, "red_score": 100}]},
            },
        ]

    def question_lists(self, category_id=None, **kwargs):
        return [
            {
                "id": "KVlvgNuOwJXPAyAQ",
                "title": "宇宙微波背景辐射",
                "category": "Misc",
                "type_id": 1,
                "red_score": 100,
                "content": '见附件<p><a href="https://pan.baidu.com/s/abc" target="_blank">附件下载</a> 提取码（GAME）</p>',
                "file_url": "https://g.ichunqiu.com/files/misc.zip",
            },
            {
                "id": "easytl",
                "title": "easytl",
                "category": "PWN",
                "type_id": 2,
                "image_url": "eci_2026qwb_easytl",
                "red_score": 100,
                "content": "目标端口约两分钟初始化",
            },
        ]

    def docker_info(self, question_id):
        if question_id == "easytl":
            return {"uuid": "u-1", "url": "10.0.1.2:9999", "ssh": {}}
        return {}

    def submit_flag(self, question_id, flag):
        self.submitted.append((question_id, flag))
        if self.fail_submit:
            raise self.fail_submit


def _adapter():
    return IchunqiuAdapter(
        _FakeClient(),
        FieldMapping(platform="ichunqiu", list_url=BASE),
    )


def test_fetch_challenges_merges_listing_detail_and_container_state():
    challenges = _adapter().fetch_challenges()

    assert [item.external_id for item in challenges] == ["KVlvgNuOwJXPAyAQ", "easytl"]
    misc, pwn = challenges
    assert misc.category == "misc"
    assert "见附件" in misc.description
    assert "附件下载: https://pan.baidu.com/s/abc" in misc.description
    assert "<p>" not in misc.description
    assert misc.attachment_urls == [
        "https://g.ichunqiu.com/files/misc.zip",
        "https://pan.baidu.com/s/abc",
    ]
    assert misc.remote is False
    assert pwn.category == "pwn"
    assert pwn.remote is True


def test_submit_maps_platform_messages_to_verdicts():
    adapter = _adapter()
    assert adapter.submit("KVlvgNuOwJXPAyAQ", "flag{ok}") == {"verdict": "correct"}
    assert adapter.client.submitted == [("KVlvgNuOwJXPAyAQ", "flag{ok}")]

    adapter.client.fail_submit = IchunqiuError("i春秋 /match/flag/submit failed: 答案错误")
    assert adapter.submit("KVlvgNuOwJXPAyAQ", "nope")["verdict"] == "wrong"

    adapter.client.fail_submit = IchunqiuError("操作太过频繁")
    assert adapter.submit("KVlvgNuOwJXPAyAQ", "nope")["verdict"] == "rate_limited"

    adapter.client.fail_submit = IchunqiuLoginError("请登录平台")
    assert adapter.submit("KVlvgNuOwJXPAyAQ", "nope")["verdict"] == "auth_required"


def test_preflight_refuses_a_missing_token_before_touching_the_platform():
    adapter = _adapter()
    adapter.client.token = ""
    with pytest.raises(IchunqiuLoginError):
        adapter.preflight()

    adapter.client.token = "not_login:abc"
    with pytest.raises(IchunqiuLoginError):
        adapter.preflight()


def test_instance_lifecycle_reads_the_container_entry():
    adapter = _adapter()
    adapter.fetch_challenges()
    assert adapter.instances() == [
        {
            "external_id": "easytl",
            "state": "ready",
            "entry": "http://10.0.1.2:9999",
            "url": "http://10.0.1.2:9999",
            "uuid": "u-1",
        }
    ]
    assert adapter.instance_limit() == 2


def test_connection_hints_are_normalised_to_host_port():
    entry = IchunqiuAdapter._entry_of
    assert entry({"url": "nc 39.106.53.101 34846"}) == "39.106.53.101:34846"
    assert entry({"url": "39.106.53.101:34846"}) == "http://39.106.53.101:34846"
    assert entry({"url": "http://1.2.3.4:8080/login"}) == "http://1.2.3.4:8080/login"
    assert entry({"url": ""}) == ""


def test_workflow_preset_builds_a_native_spec():
    spec = IchunqiuWorkflowInput(url_key="2026qwbs10").workflow_spec()
    assert spec.template_id == "ichunqiu"
    assert spec.challenges.platform == "ichunqiu"
    assert spec.competition_id == "2026qwbs10"
    assert spec.challenges.list_url == BASE
    assert "ichunqiu_token" in spec.allowed_secret_names()


def test_workflow_preset_rejects_a_full_url_as_slug():
    with pytest.raises(ValueError):
        IchunqiuWorkflowInput(url_key="https://match.ichunqiu.com/2026qwbs10")

class _SolvedClient(_FakeClient):
    """Same match, but with per-challenge solve counters like the live API."""

    def question_lists(self, category_id=None, **kwargs):
        return [
            {
                "id": "vmshare",
                "title": "vmshare",
                "category": "Crypto",
                "type_id": 1,
                "red_score": 300,
                "red_solved": {"is_solved": False, "solved_number": 411},
            },
            {
                "id": "cwp",
                "title": "cwp",
                "category": "Web",
                "type_id": 2,
                "image_url": "eci_2026qwb_cwp",
                "red_score": 500,
                "red_solved": {"is_solved": False, "solved_number": 911},
            },
            {
                "id": "easytl",
                "title": "easytl",
                "category": "PWN",
                "type_id": 1,
                "red_score": 400,
                "red_solved": {"is_solved": False, "solved_number": 515},
            },
        ]


def _attachment_adapter():
    return IchunqiuAdapter(
        _SolvedClient(),
        FieldMapping(platform="ichunqiu", list_url=BASE, attachments_only=True),
    )


def test_attachments_only_skips_containers_and_orders_by_solves():
    challenges = _attachment_adapter().fetch_challenges()

    assert [item.external_id for item in challenges] == ["easytl", "vmshare"]
    assert all(item.remote is False for item in challenges)
    assert [item.platform_data["solve_count"] for item in challenges] == [515, 411]


def test_attachments_only_never_touches_a_container():
    adapter = _attachment_adapter()
    adapter.fetch_challenges()

    assert adapter.supports_instances is False
    assert adapter.instance_limit() is None
    assert adapter.instances() == []
    with pytest.raises(IchunqiuError):
        adapter.start_instance("easytl")


def test_workflow_preset_can_stay_attachment_only():
    spec = IchunqiuWorkflowInput(url_key="2026qwbs10", attachments_only=True).workflow_spec()
    assert spec.challenges.attachments_only is True
    assert spec.challenges.to_field_mapping({}).attachments_only is True
    assert IchunqiuWorkflowInput(url_key="2026qwbs10").workflow_spec().challenges.attachments_only is False

def test_excluded_challenges_are_never_imported():
    adapter = IchunqiuAdapter(
        _SolvedClient(),
        FieldMapping(platform="ichunqiu", list_url=BASE, exclude=["easytl", "VMSHARE"]),
    )

    assert [item.external_id for item in adapter.fetch_challenges()] == ["cwp"]


def test_workflow_preset_carries_the_exclusion_list():
    spec = IchunqiuWorkflowInput(
        url_key="2026qwbs10", exclude=[" 宇宙微波背景辐射 ", ""]
    ).workflow_spec()
    assert spec.challenges.exclude == ["宇宙微波背景辐射"]
    assert spec.challenges.to_field_mapping({}).exclude == ["宇宙微波背景辐射"]


def test_unknown_submit_failures_are_never_wrong_answers():
    adapter = _adapter()
    for message in (
        "i春秋 request failed: connection reset",
        "i春秋 HTTP 500 for /match/flag/submit",
        "i春秋 /match/flag/submit failed: unexpected response",
    ):
        adapter.client.fail_submit = IchunqiuError(message)
        assert adapter.submit("easytl", "flag{unknown}")["verdict"] == "unknown"


def test_category_wildcard_and_explicit_override():
    adapter = IchunqiuAdapter(
        _FakeClient(),
        FieldMapping(
            platform="ichunqiu",
            list_url=BASE,
            category_map={"*": "crypto", "easytl": "pwn"},
        ),
    )

    challenges = {item.external_id: item for item in adapter.fetch_challenges()}

    assert challenges["KVlvgNuOwJXPAyAQ"].category == "crypto"
    assert challenges["easytl"].category == "pwn"


def test_new_adapter_lazily_restores_instances_and_match_quota():
    adapter = _adapter()

    assert adapter.instances()[0]["external_id"] == "easytl"

    adapter.client.match = {}

    def resolve_match():
        adapter.client.match = {"team_docker_limit": 3}
        return adapter.client.match

    adapter.client.resolve_match = resolve_match
    assert adapter.instance_limit() == 3


def test_template_does_not_advertise_async_submission_queries():
    from backend.platform.presets import workflow_preset

    assert "query" not in workflow_preset("ichunqiu")["capabilities"]
