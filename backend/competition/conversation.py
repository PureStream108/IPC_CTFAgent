"""Native streamed tool conversations. Persist returned provider messages intact.

The runtime owns replay and side-effect fencing; this adapter performs one
model turn and never silently retries after receiving part of a response.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from backend.members.adapters import _anthropic_endpoint, _openai_endpoint


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    messages: list[dict]
    tools: list[ToolCall] = field(default_factory=list)
    text: str = ""


def sse(response):
    lines = []
    for raw in response.iter_lines():
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line:
            if lines:
                data = "\n".join(lines)
                lines = []
                if data == "[DONE]":
                    return
                yield json.loads(data)
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
    if lines and "\n".join(lines) != "[DONE]":
        yield json.loads("\n".join(lines))


class ConversationAdapter:
    def __init__(self, config, *, request=requests.post, max_output_tokens: int = 8192):
        self.config = config
        self.request = request
        self.max_output_tokens = max_output_tokens
        self.surface = "anthropic" if config.api_format in {"anthropic", "claudecode"} else (
            "responses" if config.api_surface == "responses" else "chat_completions"
        )

    def tool_result(self, call: ToolCall, output: dict) -> dict:
        text = json.dumps(output, ensure_ascii=False)
        if self.surface == "responses":
            return {"type": "function_call_output", "call_id": call.id, "output": text}
        if self.surface == "anthropic":
            return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": text}]}
        return {"role": "tool", "tool_call_id": call.id, "content": text}

    def turn(self, messages: list[dict], system: str, tools: list[dict],
             emit: Callable[[str], None], cancel: threading.Event) -> Turn:
        config = self.config
        if config.api_format == "mock":
            return Turn([{"role": "assistant", "content": "Mock has no queued action."}], text="Mock has no queued action.")
        headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
        body: dict[str, Any] = {"model": config.model, "stream": True}
        if self.surface == "anthropic":
            headers.update({"x-api-key": config.api_key, "anthropic-version": "2023-06-01"})
            endpoint = _anthropic_endpoint(config.base_url)
            body.update(system=system, messages=messages, max_tokens=self.max_output_tokens,
                        tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools])
        elif self.surface == "responses":
            endpoint = _openai_endpoint(config.base_url, "responses")
            body.update(input=messages, instructions=system, store=False,
                        include=["reasoning.encrypted_content"],
                        tools=[{"type": "function", **t, "strict": False} for t in tools])
            if config.reasoning_effort not in {"auto", "none", "minimal"}:
                body["reasoning"] = {"effort": config.reasoning_effort}
        else:
            endpoint = _openai_endpoint(config.base_url, "chat_completions")
            body.update(messages=[{"role": "system", "content": system}, *messages],
                        tools=[{"type": "function", "function": t} for t in tools])
            if config.reasoning_effort not in {"auto", "none", "minimal"} and config.api_format != "deepseek":
                body["reasoning_effort"] = config.reasoning_effort
        if cancel.is_set():
            raise InterruptedError("session cancelled")
        with self.request(endpoint, headers=headers, json=body, stream=True, timeout=(15, 90)) as response:
            response.raise_for_status()
            return self._read(response, emit, cancel)

    def _read(self, response, emit, cancel):
        text = ""
        calls: dict[int, dict] = {}
        blocks: dict[int, dict] = {}
        output = []
        complete = False
        for event in sse(response):
            if cancel.is_set():
                raise InterruptedError("session cancelled")
            delta_text = ""
            kind = event.get("type", "")
            if "error" in event or kind in {"error", "response.failed", "response.incomplete"}:
                raise RuntimeError("provider stream did not complete successfully")
            if self.surface == "responses":
                if kind == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                elif kind == "response.completed":
                    output = event["response"]["output"]
                    complete = True
            elif self.surface == "anthropic":
                index = event.get("index", 0)
                if kind == "content_block_start":
                    blocks[index] = dict(event["content_block"])
                    if blocks[index].get("type") == "tool_use":
                        blocks[index]["_json"] = ""
                elif kind == "content_block_delta":
                    delta = event["delta"]
                    if delta.get("type") == "text_delta":
                        delta_text = delta.get("text", "")
                        blocks[index]["text"] = blocks[index].get("text", "") + delta_text
                    elif delta.get("type") == "input_json_delta":
                        blocks[index]["_json"] += delta.get("partial_json", "")
                    elif delta.get("type") == "thinking_delta":
                        blocks[index]["thinking"] = blocks[index].get("thinking", "") + delta.get("thinking", "")
                    elif delta.get("type") == "signature_delta":
                        blocks[index]["signature"] = blocks[index].get("signature", "") + delta.get("signature", "")
                elif kind == "message_delta" and event.get("delta", {}).get("stop_reason") in {"max_tokens", "refusal"}:
                    raise RuntimeError("provider turn ended before completing tool calls")
                elif kind == "message_stop":
                    complete = True
            else:
                for choice in event.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    delta_text += delta.get("content") or ""
                    for item in delta.get("tool_calls", []):
                        entry = calls.setdefault(item["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if item.get("id"):
                            entry["id"] = item["id"]
                        for field in ("name", "arguments"):
                            entry["function"][field] += item.get("function", {}).get(field, "")
                    reason = choice.get("finish_reason")
                    if reason in {"stop", "tool_calls"}:
                        complete = True
                    elif reason is not None:
                        raise RuntimeError(f"provider turn interrupted: {reason}")
            if delta_text:
                text += delta_text
                emit(delta_text)
        if not complete:
            raise RuntimeError("provider stream disconnected before completion")
        result_calls = []
        if self.surface == "responses":
            for item in output:
                if item.get("type") == "function_call":
                    result_calls.append(ToolCall(item["call_id"], item["name"], json.loads(item["arguments"])))
            messages = output
        elif self.surface == "anthropic":
            content = []
            for _, block in sorted(blocks.items()):
                if block.get("type") == "tool_use":
                    encoded = block.pop("_json")
                    block["input"] = json.loads(encoded) if encoded else block.get("input", {})
                    result_calls.append(ToolCall(block["id"], block["name"], block["input"]))
                content.append(block)
            messages = [{"role": "assistant", "content": content}]
        else:
            message = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = [entry for _, entry in sorted(calls.items())]
                for entry in message["tool_calls"]:
                    result_calls.append(ToolCall(entry["id"], entry["function"]["name"], json.loads(entry["function"]["arguments"])))
            messages = [message]
        if len({call.id for call in result_calls}) != len(result_calls) or any(not c.id or not isinstance(c.arguments, dict) for c in result_calls):
            raise ValueError("invalid or duplicate tool call identifiers")
        return Turn(messages, result_calls, text)
