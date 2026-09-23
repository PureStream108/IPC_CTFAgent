from types import SimpleNamespace

import pytest

from backend.core.ipc import requires_platform_verdict
from backend.ops.models import FlagSubmitSpec
from backend.platform.verdict import cooldown_seconds, interpret_response


def response(code, payload=None, headers=None):
    return SimpleNamespace(status_code=code, json=lambda: payload, headers=headers or {})


def spec(**kwargs):
    return FlagSubmitSpec(url="https://ctf.example/submit", **kwargs)


@pytest.mark.parametrize("code", [200, 201, 202, 204])
def test_http_success_without_evidence_is_unknown(code):
    assert interpret_response(response(code, {"success": True}), spec()).status == "unknown"


def test_explicit_correct_wrong_pending_and_submission_id():
    config = spec(success_path="data.verdict", success_values=["correct"],
                  wrong_values=["wrong"], pending_values=["queued"], submission_id_path="data.id")
    for value, expected in [("correct", "correct"), ("wrong", "wrong"), ("queued", "pending"), ("other", "unknown")]:
        verdict = interpret_response(response(200, {"data": {"verdict": value, "id": 42}}), config)
        assert verdict.status == expected
        assert verdict.submission_id == "42"


def test_boolean_does_not_match_numeric_code():
    assert interpret_response(response(200, {"success": 1}), spec(success_path="success", success_values=[True])).status == "unknown"


def test_overlapping_verdict_values_rejected():
    with pytest.raises(ValueError, match="disjoint"):
        spec(success_path="status", success_values=["ok"], wrong_values=["ok"])


def test_html_auth_expiry_and_retry_after():
    assert interpret_response(response(401), spec()).status == "auth_required"
    assert interpret_response(response(429, headers={"Retry-After": "120"}), spec()).retry_after == 120
    assert interpret_response(response(200, "<html>login</html>"), spec(success_path="verdict")).status == "unknown"


def test_cooldown_is_capped():
    assert [cooldown_seconds(i) for i in range(8)] == [0, 30, 60, 90, 120, 150, 300, 300]


def test_all_linked_platforms_require_a_verdict():
    for platform in ("gzctf", "ret2shell", "http_json"):
        assert requires_platform_verdict({"platform": platform, "external_id": "1"})
    assert not requires_platform_verdict({"platform": None, "external_id": "1"})
