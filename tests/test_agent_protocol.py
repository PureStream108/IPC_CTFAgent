from __future__ import annotations

import warnings

import pytest

from backend.core.config import LLMConfig
from backend.core.json_compat import json_dict_candidates
from backend.members.adapters import (
    ClaudeAdapter,
    MemberAction,
    ModelReply,
    ModelToolCall,
    OpenAICompatibleAdapter,
    _openai_response,
    decode_member_action,
)
from backend.ops.service import (
    _append_tool_result_history,
    _parse_chat_response,
    _parse_tool_call,
)


NATIVE_TOOLS = [
    {
        "name": "task_sandbox_health",
        "description": "Check a sandbox.",
        "parameters": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    }
]


class FakeResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        (
            '```jsonc\n{// comment\n"action":"bash","arguments":{"command":"id",},}\n```',
            "bash",
            {"command": "id"},
        ),
        (
            "action: shell\nargs:\n  command: whoami",
            "bash",
            {"command": "whoami"},
        ),
        (
            {"response": {"next_action": {"kind": "SEARCH-MEMORY", "parameters": {"query": "rsa"}}}},
            "memory",
            {"query": "rsa"},
        ),
        (
            {"bash": {"cmd": ["pwd", "id"]}},
            "bash",
            {"command": "pwd && id"},
        ),
        (
            {
                "action": "tool",
                "arguments": {
                    "server": "browser",
                    "name": "navigate",
                    "args": {"url": "https://example.test"},
                },
            },
            "tool",
            {
                "server": "browser",
                "tool": "navigate",
                "args": {"url": "https://example.test"},
            },
        ),
        (
            {
                "action": "tool",
                "server": "infra",
                "tool": "connect",
                "arguments": {"server": "target.internal", "tool": "ssh", "port": 22},
            },
            "tool",
            {
                "server": "infra",
                "tool": "connect",
                "args": {"server": "target.internal", "tool": "ssh", "port": 22},
            },
        ),
        (
            {
                "action": "tool",
                "server": "browser",
                "tool": "cookies",
                "arguments": '{"includeValues":true}',
            },
            "tool",
            {
                "server": "browser",
                "tool": "cookies",
                "args": {"includeValues": True},
            },
        ),
    ],
)
def test_member_protocol_accepts_common_model_variations(raw, kind, expected):
    action = decode_member_action(raw)

    assert action.kind == kind
    assert action.args == expected


def test_member_protocol_skips_invalid_draft_before_valid_action():
    raw = 'draft {"action":"not-real"}\nfinal {"action":"done","reason":"ok"}'

    action = decode_member_action(raw)

    assert action == MemberAction(kind="done", args={"reason": "ok"})


def test_member_protocol_accepts_native_action_tool_and_double_encoded_arguments():
    reply = ModelReply(
        tool_calls=[
            ModelToolCall(
                "ipc_action",
                '"{\\"action\\":\\"conclude\\",\\"description\\":\\"confirmed fact\\"}"',
            )
        ]
    )

    action = decode_member_action(reply)

    assert action.kind == "conclude"
    assert action.args["description"] == "confirmed fact"


def test_member_protocol_never_completes_a_truncated_string():
    with pytest.raises(ValueError, match="no JSON action"):
        decode_member_action('{"action":"bash","command":"echo unsafe')


def test_relaxed_parser_does_not_leak_literal_eval_syntax_warnings():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        json_dict_candidates(r'{"action":"done","reason":"\q"}')

    assert not [item for item in caught if issubclass(item.category, SyntaxWarning)]


def test_relaxed_parser_rejects_recursive_yaml_aliases():
    raw = "action: tool\nserver: infra\ntool: inspect\nargs: &loop\n  self: *loop"

    with pytest.raises(ValueError, match="no JSON action"):
        decode_member_action(raw)


def test_relaxed_parser_normalizes_yaml_scalars_to_json_values():
    action = decode_member_action(
        "action: tool\nserver: infra\ntool: inspect\nargs:\n  observed_at: 2026-08-11"
    )

    assert action.args["args"]["observed_at"] == "2026-08-11"


def test_member_protocol_rejects_ambiguous_parallel_actions():
    reply = ModelReply(
        tool_calls=[
            ModelToolCall("bash", {"command": "id"}),
            ModelToolCall("bash", {"command": "whoami"}),
        ]
    )

    with pytest.raises(ValueError, match="exactly one"):
        decode_member_action(reply)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"action": "bash", "thought": "run it"}, "non-empty command"),
        ({"action": "tool", "server": "browser"}, "non-empty tool"),
        ({"action": "flag", "flag": "  "}, "non-empty flag"),
    ],
)
def test_member_protocol_rejects_missing_execution_contract_fields(raw, message):
    with pytest.raises(ValueError, match=message):
        decode_member_action(raw)


def test_openai_chat_native_tool_call_is_a_member_action(monkeypatch):
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "submit_action",
                                "arguments": '{"action":"bash","command":"id"}',
                            },
                        }
                    ],
                },
            }
        ]
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: FakeResponse(payload))
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="model",
        )
    )

    action = adapter.decide({"step": 1})

    assert action.kind == "bash"
    assert action.args == {"command": "id"}


def test_action_repair_keeps_invalid_assistant_turn_and_validation_error(monkeypatch):
    payloads = iter(
        [
            {"choices": [{"message": {"content": '{"action":"not-real"}'}}]},
            {"choices": [{"message": {"content": '{"action":"done","reason":"fixed"}'}}]},
        ]
    )
    requests_seen: list[dict] = []

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return FakeResponse(next(payloads))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="model",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"
    repair_messages = requests_seen[1]["messages"]
    assert any(
        message["role"] == "assistant" and '"action":"not-real"' in message["content"]
        for message in repair_messages
    )
    assert any(
        message["role"] == "user" and "validator error" in message["content"]
        for message in repair_messages
    )


def test_missing_bash_command_is_repaired_before_dispatch(monkeypatch):
    payloads = iter(
        [
            {"choices": [{"message": {"content": '{"action":"bash","thought":"run"}'}}]},
            {"choices": [{"message": {"content": '{"action":"bash","command":"id"}'}}]},
        ]
    )
    requests_seen: list[dict] = []

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return FakeResponse(next(payloads))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://gateway.invalid/v1",
            model="model",
        )
    )

    action = adapter.decide({"step": 1})

    assert action.args == {"command": "id"}
    assert len(requests_seen) == 2
    assert "non-empty command" in requests_seen[1]["messages"][-1]["content"]


def test_chat_response_accepts_legacy_text_and_deduplicates_mirrored_tool_calls():
    call = {
        "id": "call_legacy",
        "type": "tool_call",
        "name": "ipc_action",
        "arguments": '{"action":"done","reason":"ok"}',
    }
    reply = _openai_response(
        {
            "choices": [
                {
                    "text": '{"action":"done","reason":"legacy"}',
                    "message": {"content": [call], "tool_calls": [call]},
                }
            ]
        },
        "chat_completions",
    )

    assert reply.text == '{"action":"done","reason":"legacy"}'
    assert len(reply.tool_calls) == 1
    assert decode_member_action(reply).kind == "done"

    nested_reply = _openai_response(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_call",
                                "tool_call": {
                                    "id": "call_nested",
                                    "function": {
                                        "name": "ipc_action",
                                        "arguments": '{"action":"memory","query":"rsa"}',
                                    },
                                },
                            }
                        ]
                    }
                }
            ]
        },
        "chat_completions",
    )
    assert decode_member_action(nested_reply).kind == "memory"


def test_openai_responses_native_function_call_is_a_member_action(monkeypatch):
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_2",
                "name": "ipc_action",
                "arguments": '{"action":"done","reason":"complete"}',
            }
        ],
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: FakeResponse(payload))
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="responses",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.decide({"step": 1}).kind == "done"


def test_anthropic_tool_use_is_a_member_action(monkeypatch):
    payload = {
        "content": [
            {
                "type": "tool_use",
                "id": "tool_1",
                "name": "ipc_action",
                "input": {"action": "memory_search", "query": "heap exploitation"},
            }
        ],
        "stop_reason": "tool_use",
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: FakeResponse(payload))
    adapter = ClaudeAdapter(
        LLMConfig(
            api_format="anthropic",
            api_key="key",
            base_url="https://anthropic.invalid/v1/messages",
            model="claude-model",
        )
    )

    action = adapter.decide({"step": 1})

    assert action.kind == "memory"
    assert action.args == {"query": "heap exploitation"}


@pytest.mark.parametrize(
    ("surface", "payload"),
    [
        ("chat_completions", {"choices": [{"message": {"content": "done"}}]}),
        (
            "responses",
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
        ),
    ],
)
def test_openai_complete_sends_surface_specific_native_tools(monkeypatch, surface, payload):
    bodies: list[dict] = []

    def fake_post(*args, **kwargs):
        bodies.append(kwargs["json"])
        return FakeResponse(payload)

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface=surface,
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )

    assert adapter.complete(
        [{"role": "user", "content": "check"}], tools=NATIVE_TOOLS
    ).text == "done"
    function = bodies[0]["tools"][0]
    assert function["type"] == "function"
    if surface == "chat_completions":
        function = function["function"]
    assert function["name"] == "task_sandbox_health"
    assert function["parameters"]["required"] == ["project_id"]
    assert function["strict"] is False


def test_responses_tool_loop_replays_reasoning_call_and_linked_output(monkeypatch):
    payloads = iter(
        [
            {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "task_sandbox_health",
                        "arguments": '{"project_id":"p1"}',
                    },
                ],
            },
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "healthy"}],
                    }
                ],
            },
        ]
    )
    bodies: list[dict] = []

    def fake_post(*args, **kwargs):
        bodies.append(kwargs["json"])
        return FakeResponse(next(payloads))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="responses",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )
    messages = [{"role": "user", "content": "check p1"}]
    first = adapter.complete(messages, tools=NATIVE_TOOLS)
    _append_tool_result_history(
        messages,
        completion=first,
        tool_name="task_sandbox_health",
        tool_arguments={"project_id": "p1"},
        safe_tool_result='{"ok":true}',
        round_index=0,
        known_secret_values={},
    )

    assert adapter.complete(messages, tools=NATIVE_TOOLS).text == "healthy"
    second_input = bodies[1]["input"]
    assert {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"} in second_input
    assert any(
        item.get("type") == "function_call" and item.get("call_id") == "call_1"
        for item in second_input
    )
    assert any(
        item.get("type") == "function_call_output"
        and item.get("call_id") == "call_1"
        and "TOOL_RESULT task_sandbox_health" in item.get("output", "")
        for item in second_input
    )


def test_chat_completions_tool_loop_replays_assistant_call_and_tool_result(monkeypatch):
    payloads = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_chat_1",
                                    "type": "function",
                                    "function": {
                                        "name": "task_sandbox_health",
                                        "arguments": '{"project_id":"p1"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "healthy"}}]},
        ]
    )
    bodies: list[dict] = []

    def fake_post(*args, **kwargs):
        bodies.append(kwargs["json"])
        return FakeResponse(next(payloads))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = OpenAICompatibleAdapter(
        LLMConfig(
            api_format="openai",
            api_surface="chat_completions",
            api_key="key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
        )
    )
    messages = [{"role": "user", "content": "check p1"}]
    first = adapter.complete(messages, tools=NATIVE_TOOLS)
    _append_tool_result_history(
        messages,
        completion=first,
        tool_name="task_sandbox_health",
        tool_arguments={"project_id": "p1"},
        safe_tool_result='{"ok":true}',
        round_index=0,
        known_secret_values={},
    )

    assert adapter.complete(messages, tools=NATIVE_TOOLS).text == "healthy"
    second_messages = bodies[1]["messages"]
    assistant = second_messages[-2]
    result = second_messages[-1]
    assert assistant["tool_calls"][0]["id"] == "call_chat_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "task_sandbox_health"
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_chat_1"


def test_anthropic_native_tool_loop_preserves_tool_use_id(monkeypatch):
    payloads = iter(
        [
            {
                "content": [
                    {"type": "thinking", "thinking": "opaque", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "task_sandbox_health",
                        "input": {"project_id": "p1"},
                    },
                ],
                "stop_reason": "tool_use",
            },
            {"content": [{"type": "text", "text": "healthy"}], "stop_reason": "end_turn"},
        ]
    )
    bodies: list[dict] = []

    def fake_post(*args, **kwargs):
        bodies.append(kwargs["json"])
        return FakeResponse(next(payloads))

    monkeypatch.setattr("requests.post", fake_post)
    adapter = ClaudeAdapter(
        LLMConfig(
            api_format="anthropic",
            api_key="key",
            base_url="https://api.anthropic.invalid/v1",
            model="claude-model",
        )
    )
    messages = [{"role": "user", "content": "check p1"}]
    first = adapter.complete(messages, tools=NATIVE_TOOLS)
    _append_tool_result_history(
        messages,
        completion=first,
        tool_name="task_sandbox_health",
        tool_arguments={"project_id": "p1"},
        safe_tool_result='{"ok":true}',
        round_index=0,
        known_secret_values={},
    )

    assert adapter.complete(messages, tools=NATIVE_TOOLS).text == "healthy"
    assert bodies[0]["tools"][0]["input_schema"]["required"] == ["project_id"]
    assert bodies[1]["messages"][1]["content"][0]["type"] == "thinking"
    result = bodies[1]["messages"][2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tool_1"


def test_ops_protocol_uses_the_same_tolerant_decoder():
    parsed = _parse_chat_response(
        """```jsonc
        {
          // compatible reply
          "answer": "done",
          "tool_call": {
            "function": {
              "name": "host_exec",
              "arguments": "{command: 'id', timeout: 15,}",
            },
          },
        }
        ```"""
    )

    assert parsed["reply"] == "done"
    assert _parse_tool_call(parsed["tool_call"]) == (
        "host_exec",
        {"command": "id", "timeout": 15},
    )
    with pytest.raises(ValueError, match="exactly one"):
        _parse_tool_call(
            [
                {"name": "host_exec", "arguments": {"command": "id"}},
                {"name": "host_exec", "arguments": {"command": "whoami"}},
            ]
        )


def test_ops_protocol_accepts_case_aliases_content_and_empty_tool_list():
    assert _parse_chat_response('{"Reply":"case-safe","Tool-Calls":[]}')["reply"] == "case-safe"
    assert _parse_chat_response('{"content":"content-safe"}')["reply"] == "content-safe"
    assert _parse_chat_response('{"response":"response-safe"}')["reply"] == "response-safe"
    assert _parse_chat_response('"double-encoded reply"')["reply"] == "double-encoded reply"
    assert _parse_tool_call([]) is None
    assert _parse_tool_call(
        {
            "Function": {
                "Name": "host_exec",
                "Arguments": '{"command":"id"}',
            }
        }
    ) == ("host_exec", {"command": "id"})
    assert _parse_tool_call(
        '"{\\"name\\":\\"host_exec\\",\\"arguments\\":{\\"command\\":\\"id\\"}}"'
    ) == ("host_exec", {"command": "id"})
