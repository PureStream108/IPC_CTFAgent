"""The health page proves which model the gateway routes, not just that it answers."""

from __future__ import annotations

from backend.core.config import LLMConfig
from backend.members.adapters import OpenAICompatibleAdapter, _models_endpoint


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _adapter(model="deepseek-v4.1-flash"):
    return OpenAICompatibleAdapter(LLMConfig(
        api_format="openai", api_key="k",
        base_url="https://opencode.ai/zen/go/v1/", model=model,
    ))


def test_models_endpoint_appends_models_to_the_plan_root():
    assert _models_endpoint("https://opencode.ai/zen/go/v1/") == (
        "https://opencode.ai/zen/go/v1/models"
    )
    assert _models_endpoint("https://api.example/v1/chat/completions") == (
        "https://api.example/v1/models"
    )


def test_catalog_lists_the_configured_model(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers=None, timeout=None: _Response(
            {"data": [{"id": "deepseek-v4.1-flash"}, {"id": "kimi-k2.7-code"}]}
        ),
    )
    catalog = _adapter().model_catalog()
    assert catalog["checked"] is True
    assert catalog["model_listed"] is True
    assert catalog["count"] == 2


def test_a_missing_model_is_a_health_failure(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers=None, timeout=None: _Response({"data": [{"id": "other"}]}),
    )
    adapter = _adapter("does-not-exist")
    adapter._request_compatible = lambda *args, **kwargs: "OK"
    health = adapter.health()
    assert health["ok"] is False
    assert "not listed" in health["error"]
    assert health["catalog"]["model_listed"] is False


def test_a_rejected_catalog_request_does_not_mark_a_good_model_bad(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers=None, timeout=None: _Response({}, status_code=404),
    )
    adapter = _adapter()
    adapter._request_compatible = lambda *args, **kwargs: "OK"
    health = adapter.health()
    assert health["ok"] is True
    assert health["catalog"]["checked"] is False
