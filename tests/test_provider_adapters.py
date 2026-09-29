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


# ---------------------------------------------------------------------------
# Streaming usage capture and provider session affinity (unified runtime).


class _StreamResponse:
    """Minimal SSE response for ConversationAdapter."""

    def __init__(self, payloads, *, done: bool = True):
        self.payloads = payloads
        self.done = done

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for payload in self.payloads:
            yield ("data: " + payload).encode()
            yield b""
        if self.done:
            yield b"data: [DONE]"
            yield b""


def _conversation(api_format="openai", surface="auto", **kwargs):
    from backend.competition.conversation import ConversationAdapter

    return ConversationAdapter(
        LLMConfig(
            api_format=api_format,
            api_surface=surface,
            api_key="k",
            base_url="https://provider.invalid/v1",
            model="m",
        ),
        **kwargs,
    )


def _run(adapter, request):
    import threading

    adapter.request = request
    return adapter.turn(
        [{"role": "user", "content": "go"}], "system", [], lambda _t: None,
        threading.Event(),
    )


def test_chat_completions_usage_is_requested_and_parsed():
    """A streamed chat completion omits usage unless stream_options asks."""
    bodies = []

    def request(url, **kwargs):
        bodies.append(kwargs["json"])
        return _StreamResponse([
            '{"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}',
            '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            '{"choices":[],"usage":{"prompt_tokens":1200,"completion_tokens":34,'
            '"completion_tokens_details":{"reasoning_tokens":12}}}',
        ])

    turn = _run(_conversation(), request)

    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert turn.usage == {
        "prompt_tokens": 1200, "output_tokens": 34, "reasoning_tokens": 12
    }


def test_stream_options_are_dropped_when_the_provider_rejects_them():
    """Losing usage reporting is acceptable; failing the turn is not."""
    attempts = []

    class Rejecting:
        status_code = 400
        text = "Unrecognized request argument supplied: stream_options"

    def request(url, **kwargs):
        attempts.append(dict(kwargs["json"]))
        if "stream_options" in kwargs["json"]:
            error = requests.HTTPError("400 Bad Request")
            error.response = Rejecting()
            raise error
        return _StreamResponse([
            '{"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        ])

    adapter = _conversation()
    turn = _run(adapter, request)

    assert len(attempts) == 2
    assert "stream_options" in attempts[0]
    assert "stream_options" not in attempts[1]
    assert turn.text == "ok"
    assert turn.usage is None
    # The degradation sticks, so the next turn does not repeat the failure.
    assert adapter._stream_usage_supported is False
    _run(adapter, request)
    assert "stream_options" not in attempts[2]


def test_an_unrelated_400_is_not_swallowed_as_a_stream_options_problem():
    class Rejecting:
        status_code = 400
        text = "model not found"

    def request(url, **kwargs):
        error = requests.HTTPError("400 Bad Request")
        error.response = Rejecting()
        raise error

    with pytest.raises(requests.HTTPError):
        _run(_conversation(), request)


def test_anthropic_usage_is_merged_across_start_and_delta_events():
    """Anthropic splits input and output counts across two events."""
    def request(url, **kwargs):
        return _StreamResponse(
            [
                '{"type":"message_start","message":{"usage":{"input_tokens":900,'
                '"cache_read_input_tokens":100}}}',
                '{"type":"content_block_start","index":0,'
                '"content_block":{"type":"text","text":""}}',
                '{"type":"content_block_delta","index":0,'
                '"delta":{"type":"text_delta","text":"hello"}}',
                '{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                '"usage":{"output_tokens":42}}',
                '{"type":"message_stop"}',
            ],
            done=False,
        )

    turn = _run(_conversation(api_format="anthropic"), request)

    # Cached prompt tokens still occupy the context window, so they count.
    assert turn.usage["prompt_tokens"] == 1000
    assert turn.usage["output_tokens"] == 42
    assert turn.text == "hello"


def test_responses_usage_is_read_from_the_completed_event():
    def request(url, **kwargs):
        return _StreamResponse(
            [
                '{"type":"response.output_text.delta","delta":"done"}',
                '{"type":"response.completed","response":{"output":[],'
                '"usage":{"input_tokens":777,"output_tokens":21,'
                '"output_tokens_details":{"reasoning_tokens":9}}}}',
            ],
            done=False,
        )

    turn = _run(_conversation(surface="responses"), request)

    assert turn.usage == {
        "prompt_tokens": 777, "output_tokens": 21, "reasoning_tokens": 9
    }


def test_a_truncated_turn_still_reports_its_usage():
    from backend.competition.conversation import TurnTruncated

    def request(url, **kwargs):
        return _StreamResponse([
            '{"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}',
            '{"choices":[{"index":0,"delta":{},"finish_reason":"length"}],'
            '"usage":{"prompt_tokens":50,"completion_tokens":4096}}',
        ])

    with pytest.raises(TurnTruncated) as caught:
        _run(_conversation(), request)

    assert caught.value.turn.usage["output_tokens"] == 4096


def test_provider_session_id_is_stable_across_rebuilt_adapters():
    """Session affinity must survive a reconnect, not be reinvented."""
    durable = "ipc-session-abc123"
    first = _conversation(provider_session_id=durable)
    second = _conversation(provider_session_id=durable)
    assert first.provider_session_id == second.provider_session_id == durable
    # Without an explicit id each adapter still gets its own.
    assert _conversation().provider_session_id != _conversation().provider_session_id


def test_member_adapter_accepts_a_durable_provider_session_id():
    durable = "ipc-agent-stable99"
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai", api_key="k",
            base_url="https://provider.invalid/v1", model="m",
        ),
        name="amber",
        provider_session_id=durable,
    )
    assert adapter.provider_session_id == durable


def test_output_budget_and_read_timeout_come_from_configuration():
    captured = {}

    def request(url, **kwargs):
        captured.update(kwargs)
        return _StreamResponse([
            '{"type":"message_start","message":{"usage":{"input_tokens":1}}}',
            '{"type":"message_stop"}',
        ], done=False)

    adapter = _conversation(
        api_format="anthropic", max_output_tokens=32768, read_timeout=300
    )
    _run(adapter, request)

    assert captured["json"]["max_tokens"] == 32768
    # The read timeout is a chunk gap, so a long silent reasoning phase before
    # the first token must not kill the turn.
    assert captured["timeout"] == (15, 300)
