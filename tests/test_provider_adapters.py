from __future__ import annotations

import pytest
import requests

from backend.core.config import LLMConfig
from backend.members.adapters import (
    ClaudeAdapter,
    MemberAction,
    NonRetryableProviderError,
    OpenAICompatibleAdapter,
    RetryableProviderError,
    _extract_json,
)


class FakeResponse:
    def __init__(self, status_code: int, payload, *, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload


def _openai_adapter() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="model",
        )
    )


def test_openai_rate_limit_retries_and_honors_retry_after(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            return FakeResponse(
                429,
                {"error": {"message": "rate limited"}},
                headers={"Retry-After": "1.25"},
            )
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"action":"done","reason":"ok"}'}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)

    assert _openai_adapter().decide({"step": 1}).kind == "done"
    assert calls == 3
    assert sleeps == [1.25, 1.25]


def test_openai_server_error_exhaustion_is_typed_and_bounded(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(503, {"error": {"message": "temporarily unavailable"}})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.random.uniform", lambda low, high: high)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)

    with pytest.raises(RetryableProviderError) as caught:
        _openai_adapter().chat([{"role": "user", "content": "hello"}])

    assert calls == 3
    assert sleeps == [0.5, 1.0]
    assert caught.value.retryable is True
    assert caught.value.status_code == 503


def test_openai_timeout_retries_then_succeeds(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise requests.Timeout("upstream read timed out")
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": "recovered"}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.random.uniform", lambda low, high: high)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)

    assert _openai_adapter().chat([{"role": "user", "content": "hello"}]) == "recovered"
    assert calls == 2
    assert sleeps == [0.5]


def test_openai_auth_error_is_non_retryable(monkeypatch):
    calls = 0

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(401, {"error": {"message": "invalid API key"}})

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(NonRetryableProviderError) as caught:
        _openai_adapter().chat([{"role": "user", "content": "hello"}])

    assert calls == 1
    assert caught.value.retryable is False
    assert caught.value.status_code == 401


def test_openai_compatibility_degrade_does_not_backoff(monkeypatch):
    bodies: list[dict] = []
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        bodies.append(kwargs["json"])
        if len(bodies) == 1:
            return FakeResponse(
                400,
                {"error": {"message": "response_format json_schema is not supported"}},
            )
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"action":"done","reason":"ok"}'}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"
    assert len(bodies) == 2
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert bodies[1]["response_format"] == {"type": "json_object"}
    assert sleeps == []


def test_openai_auto_surface_fallback_remains_available(monkeypatch):
    urls: list[str] = []
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        urls.append(url)
        if url.endswith("/responses"):
            return FakeResponse(404, {"error": {"message": "unknown endpoint"}})
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"action":"done","reason":"ok"}'}}]},
        )

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="auto",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"
    assert urls == [
        "https://gateway.invalid/v1/responses",
        "https://gateway.invalid/v1/chat/completions",
    ]
    assert sleeps == []


def test_anthropic_uses_the_same_retry_policy(monkeypatch):
    calls = 0
    sleeps: list[float] = []

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FakeResponse(
                429,
                {"error": {"message": "rate limited"}},
                headers={"Retry-After": "0"},
            )
        return FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("backend.members.adapters.time.sleep", sleeps.append)
    adapter = ClaudeAdapter(
        LLMConfig(
            api_format="anthropic",
            api_key="key",
            base_url="https://anthropic.invalid/v1/messages",
            model="claude-model",
        )
    )

    assert adapter.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert calls == 2
    assert sleeps == [0.0]


def test_truncated_json_recovers_only_complete_values():
    recovered = _extract_json('analysis\n{"action":"done","reason":"complete value"')
    assert MemberAction.from_obj(recovered).kind == "done"

    semantic_failure = _extract_json('{"action":"not-a-real-action"')
    with pytest.raises(ValueError, match="invalid action kind"):
        MemberAction.from_obj(semantic_failure)

    with pytest.raises(ValueError, match="no JSON action"):
        _extract_json('{"action":"bash","command":"echo flag')
