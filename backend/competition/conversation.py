from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from backend.members.adapters import (
    _anthropic_endpoint,
    _is_reasoning_model,
    _openai_endpoint,
    opencode_provider_headers,
)

_recovery_logger = logging.getLogger(__name__)

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


_OUTPUT_LIMIT_REASONS = frozenset({"max_tokens", "max_output_tokens", "length"})

_PARTIAL_PLACEHOLDER = "[no output was produced before the limit was reached]"


def _partial_text(text: str) -> str:
    return text if text.strip() else _PARTIAL_PLACEHOLDER


def _partial_text_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": _partial_text(text)}],
    }


CONTINUATION_INSTRUCTION = (
    "Your previous turn stopped at the provider output limit, so it was cut "
    "off and no tool call from it ran. The partial text above is all that was "
    "kept. Continue from there: write less prose, and issue the next tool call "
    "directly."
)


@dataclass
class Turn:
    messages: list[dict]
    tools: list[ToolCall] = field(default_factory=list)
    text: str = ""
    usage: dict | None = None


def normalize_usage(raw: Any) -> dict | None:
    """Map a provider usage object onto prompt/output/reasoning counts.

    The three surfaces disagree on names: Anthropic reports
    ``input_tokens``/``output_tokens``, chat completions ``prompt_tokens``/
    ``completion_tokens``, and the responses API ``input_tokens`` plus nested
    ``output_tokens_details.reasoning_tokens``.
    """
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    if prompt is None:
        prompt = raw.get("input_tokens")
    output = raw.get("completion_tokens")
    if output is None:
        output = raw.get("output_tokens")
    reasoning = None
    for key in ("output_tokens_details", "completion_tokens_details"):
        details = raw.get(key)
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            reasoning = details["reasoning_tokens"]
            break
    if reasoning is None:
        reasoning = raw.get("reasoning_tokens")
    if prompt is not None:
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            extra = raw.get(key)
            if isinstance(extra, int):
                prompt += extra
    usage = {
        "prompt_tokens": prompt,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
    }
    return usage if any(value is not None for value in usage.values()) else None


def merge_usage(current: dict | None, incoming: dict | None) -> dict | None:
    """Combine partial usage reports without dropping either side.

    Anthropic splits the counts across events: ``message_start`` carries the
    input tokens and ``message_delta`` the output tokens. Replacing wholesale
    would discard whichever arrived first.
    """
    if incoming is None:
        return current
    if current is None:
        return incoming
    merged = dict(current)
    for key, value in incoming.items():
        if value is not None:
            merged[key] = value
    return merged


class TurnTruncated(RuntimeError):
    """A provider stopped mid-turn at its own output limit.

    Hitting the limit is a budget event, not a broken session: the partial
    turn is carried here so the runtime can persist what already arrived and
    ask the provider to continue, instead of failing the whole task.  The
    attached messages are always a transcript the same surface accepts on the
    next request.
    """

    def __init__(self, turn: Turn, reason: str) -> None:
        super().__init__(f"provider turn truncated at the output limit ({reason})")
        self.turn = turn
        self.reason = reason


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
    emits_reasoning = True

    def __init__(
        self,
        config,
        *,
        request=requests.post,
        max_output_tokens: int = 32768,
        read_timeout: int = 300,
        turn_timeout: float = 900,
        progress_timeout: float = 300,
        provider_session_id: str | None = None,
    ) -> None:
        self.config = config
        self.request = request
        self.max_output_tokens = max_output_tokens
        self.read_timeout = max(.1, float(read_timeout))
        self.turn_timeout = max(.1, float(turn_timeout))
        self.progress_timeout = max(.1, float(progress_timeout))
        self.surface = "anthropic" if config.api_format == "anthropic" else (
            "responses" if config.api_surface == "responses" else "chat_completions"
        )
        self._provider_session = provider_session_id or f"ipc-session-{uuid.uuid4().hex[:16]}"
        self._stream_usage_supported = True
        self.on_first_response: Callable[[], None] | None = None
        self.on_first_content: Callable[[], None] | None = None
        self.on_progress: Callable[[], None] | None = None

    @property
    def provider_session_id(self) -> str:
        return self._provider_session

    def tool_result(self, call: ToolCall, output: dict) -> dict:
        text = json.dumps(output, ensure_ascii=False)
        if self.surface == "responses":
            return {"type": "function_call_output", "call_id": call.id, "output": text}
        if self.surface == "anthropic":
            return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": text}]}
        return {"role": "tool", "tool_call_id": call.id, "content": text}

    def turn(self, messages: list[dict], system: str, tools: list[dict],
             emit: Callable[[str], None], cancel: threading.Event,
             emit_reasoning: Callable[[str], None] | None = None) -> Turn:
        config = self.config
        self._request_started_at = time.monotonic()
        if config.api_format == "mock":
            latest = ""
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    content = message.get("content")
                    latest = content if isinstance(content, str) else json.dumps(
                        content, ensure_ascii=False
                    )
                    break
            text = f"Mock Ops Agent received: {latest}"
            emit(text)
            return Turn([{"role": "assistant", "content": text}], text=text)
        headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
        headers.update(opencode_provider_headers(config.base_url, self._provider_session))
        body: dict[str, Any] = {"model": config.model, "stream": True}
        if self.surface == "anthropic":
            headers.update({"x-api-key": config.api_key, "anthropic-version": "2023-06-01"})
            endpoint = _anthropic_endpoint(config.base_url)
            body.update(system=system, messages=messages, max_tokens=self.max_output_tokens,
                        tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools])
        elif self.surface == "responses":
            endpoint = _openai_endpoint(config.base_url, "responses")
            body.update(input=messages, instructions=system, store=False,
                        max_output_tokens=self.max_output_tokens,
                        include=["reasoning.encrypted_content"],
                        tools=[{"type": "function", **t, "strict": False} for t in tools])
            if config.reasoning_effort not in {"auto", "none", "minimal"}:
                body["reasoning"] = {"effort": config.reasoning_effort}
        else:
            endpoint = _openai_endpoint(config.base_url, "chat_completions")
            body.update(messages=[{"role": "system", "content": system}, *messages],
                        tools=[{"type": "function", "function": t} for t in tools])
            token_key = "max_completion_tokens" if config.api_format != "deepseek" and _is_reasoning_model(config.model) else "max_tokens"
            body[token_key] = self.max_output_tokens
            if config.reasoning_effort not in {"auto", "none", "minimal"} and config.api_format != "deepseek":
                body["reasoning_effort"] = config.reasoning_effort
            if self._stream_usage_supported:
                body["stream_options"] = {"include_usage": True}
        if cancel.is_set():
            raise InterruptedError("session cancelled")
        think = emit_reasoning or (lambda _text: None)
        try:
            with self.request(
                endpoint, headers=headers, json=body, stream=True,
                timeout=(min(15, self.turn_timeout), min(self.read_timeout, self.turn_timeout, self.progress_timeout)),
            ) as response:
                response.raise_for_status()
                return self._read(response, emit, cancel, think)
        except requests.HTTPError as exc:
            if not self._should_drop_stream_options(exc, body):
                raise
            self._stream_usage_supported = False
            body.pop("stream_options", None)
            with self.request(
                endpoint, headers=headers, json=body, stream=True,
                timeout=(min(15, self.turn_timeout), min(self.read_timeout, self.turn_timeout, self.progress_timeout)),
            ) as response:
                response.raise_for_status()
                return self._read(response, emit, cancel, think)

    @staticmethod
    def _should_drop_stream_options(exc: requests.HTTPError, body: dict) -> bool:
        if "stream_options" not in body:
            return False
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status != 400:
            return False
        try:
            detail = response.text or ""
        except Exception:
            _recovery_logger.debug('Error handled in _should_drop_stream_options', exc_info=True)
            detail = ""
        return "stream_options" in detail or "include_usage" in detail

    def _read(self, response, emit, cancel, emit_reasoning=None):
        finished = threading.Event()
        started = getattr(self, "_request_started_at", time.monotonic())
        progress = [started]
        interrupted = []

        def abort():
            raw = getattr(response, "raw", None)
            connection = getattr(raw, "_connection", None)
            sock = getattr(connection, "sock", None)
            if sock is None:
                sock = getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            close = getattr(response, "close", None)
            if callable(close):
                close()

        def watch():
            while not finished.wait(.05):
                now = time.monotonic()
                reason = ("cancelled" if cancel.is_set() else "turn deadline" if now-started >= self.turn_timeout
                          else "no effective progress" if now-progress[0] >= self.progress_timeout else None)
                if reason:
                    interrupted.append(reason)
                    try:
                        abort()
                    except Exception:
                        _recovery_logger.debug("stream abort failed", exc_info=True)
                    return

        first_content = [False]
        last_noted = [started]
        def progressed(value):
            progress[0] = time.monotonic()
            if not first_content[0]:
                first_content[0] = True
                if self.on_first_content is not None:
                    self.on_first_content()
            if progress[0] - last_noted[0] >= 15:
                last_noted[0] = progress[0]
                if self.on_progress is not None:
                    self.on_progress()
            return value

        def text(value):
            if value:
                progressed(value)
            emit(value)

        def thinking(value):
            if value:
                progressed(value)
            if emit_reasoning:
                emit_reasoning(value)

        watcher = threading.Thread(target=watch, name="ipc-provider-deadline", daemon=True)
        watcher.start()
        try:
            result = self._read_stream(response, text, cancel, thinking, progressed)
            if interrupted:
                raise TimeoutError(interrupted[0])
            return result
        except Exception:
            if cancel.is_set():
                raise InterruptedError("session cancelled") from None
            if interrupted:
                raise TimeoutError(f"provider exceeded {interrupted[0]}") from None
            raise
        finally:
            finished.set()
            watcher.join(timeout=1)

    def _read_stream(self, response, emit, cancel, emit_reasoning=None, progressed=None):
        think = emit_reasoning or (lambda _text: None)
        text = ""
        calls: dict[int, dict] = {}
        blocks: dict[int, dict] = {}
        output = []
        complete = False
        truncated: str | None = None
        usage: dict | None = None
        first_event = True
        for event in sse(response):
            if first_event:
                first_event = False
                if self.on_first_response is not None:
                    self.on_first_response()
            if cancel.is_set():
                raise InterruptedError("session cancelled")
            delta_text = ""
            reasoning_text = ""
            kind = event.get("type", "")
            if "error" in event or kind in {"error", "response.failed"}:
                raise RuntimeError("provider stream did not complete successfully")
            if self.surface == "responses":
                if kind == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                elif kind in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                }:
                    reasoning_text = str(event.get("delta") or "")
                elif kind in {"response.completed", "response.incomplete"}:
                    payload = event.get("response") or {}
                    output = payload.get("output") or []
                    usage = merge_usage(usage, normalize_usage(payload.get("usage")))
                    complete = True
                    if kind == "response.incomplete":
                        reason = (payload.get("incomplete_details") or {}).get("reason")
                        truncated = str(reason or "incomplete")
                        if truncated not in _OUTPUT_LIMIT_REASONS:
                            raise RuntimeError(
                                "provider stream did not complete successfully"
                            )
                elif (kind in {"response.function_call_arguments.delta", "response.output_item.added"}
                      and progressed and (event.get("delta") or (event.get("item") or {}).get("type") == "function_call")):
                    progressed(event)
            elif self.surface == "anthropic":
                index = event.get("index", 0)
                if kind == "content_block_start":
                    blocks[index] = dict(event["content_block"])
                    if blocks[index].get("type") == "tool_use":
                        blocks[index]["_json"] = ""
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        delta_text = delta.get("text", "")
                        blocks[index]["text"] = blocks[index].get("text", "") + delta_text
                    elif delta.get("type") == "input_json_delta":
                        blocks[index]["_json"] += delta.get("partial_json", "")
                        if delta.get("partial_json") and progressed:
                            progressed(event)
                    elif delta.get("type") == "thinking_delta":
                        reasoning_text = delta.get("thinking", "")
                        blocks[index]["thinking"] = blocks[index].get("thinking", "") + reasoning_text
                    elif delta.get("type") == "signature_delta":
                        blocks[index]["signature"] = blocks[index].get("signature", "") + delta.get("signature", "")
                elif kind == "message_delta":
                    usage = merge_usage(usage, normalize_usage(event.get("usage")))
                    stop = (event.get("delta") or {}).get("stop_reason")
                    if stop in _OUTPUT_LIMIT_REASONS:
                        truncated = str(stop)
                    elif stop == "refusal":
                        raise RuntimeError("provider refused to complete the turn")
                elif kind == "message_start":
                    usage = merge_usage(
                        usage,
                        normalize_usage((event.get("message") or {}).get("usage")),
                    )
                elif kind == "message_stop":
                    complete = True
            else:
                usage = merge_usage(usage, normalize_usage(event.get("usage")))
                for choice in event.get("choices") or []:
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    delta_text += delta.get("content") or ""
                    for field in ("reasoning_content", "reasoning"):
                        value = delta.get(field)
                        if isinstance(value, str):
                            reasoning_text += value
                    for item in delta.get("tool_calls") or []:
                        if progressed:
                            progressed(item)
                        item = item or {}
                        entry = calls.setdefault(item.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if item.get("id"):
                            entry["id"] = item["id"]
                        function = item.get("function") or {}
                        for field in ("name", "arguments"):
                            entry["function"][field] += function.get(field) or ""
                    reason = choice.get("finish_reason")
                    if reason in {"stop", "tool_calls"}:
                        complete = True
                    elif reason in _OUTPUT_LIMIT_REASONS:
                        truncated = str(reason)
                        complete = True
                    elif reason is not None:
                        raise RuntimeError(f"provider turn interrupted: {reason}")
            if reasoning_text:
                think(reasoning_text)
            if delta_text:
                text += delta_text
                emit(delta_text)
        if not complete and truncated is None:
            raise RuntimeError("provider stream disconnected before completion")
        turn = self._assemble(text, output, blocks, calls, truncated)
        turn.usage = usage
        if truncated is not None:
            raise TurnTruncated(turn, truncated)
        return turn

    def _assemble(self, text, output, blocks, calls, truncated) -> Turn:
        """Build the turn, dropping tool calls that a truncated turn cut short.

        A truncated turn never executes its tool calls, so the partial
        assistant message is persisted without them: an assistant message
        holding unanswered tool calls is rejected by every surface on the next
        request, while a text-only partial can be followed by a continuation
        instruction.
        """
        result_calls: list[ToolCall] = []
        if self.surface == "responses":
            messages = []
            for item in output or []:
                if item.get("type") == "function_call":
                    if truncated is not None:
                        continue
                    result_calls.append(
                        ToolCall(item["call_id"], item["name"], json.loads(item["arguments"]))
                    )
                messages.append(item)
            if truncated is not None and not messages:
                messages = [_partial_text_item(text)]
        elif self.surface == "anthropic":
            content = []
            for _, block in sorted(blocks.items()):
                if block.get("type") == "tool_use":
                    encoded = block.pop("_json")
                    if truncated is not None:
                        continue
                    block["input"] = json.loads(encoded) if encoded else block.get("input", {})
                    result_calls.append(ToolCall(block["id"], block["name"], block["input"]))
                elif (
                    truncated is not None
                    and block.get("type") == "thinking"
                    and not block.get("signature")
                ):
                    continue
                content.append(block)
            if truncated is not None and not content:
                content = [{"type": "text", "text": _partial_text(text)}]
            messages = [{"role": "assistant", "content": content}]
        else:
            message = {"role": "assistant", "content": text or None}
            if calls and truncated is None:
                message["tool_calls"] = [entry for _, entry in sorted(calls.items())]
                for entry in message["tool_calls"]:
                    result_calls.append(ToolCall(entry["id"], entry["function"]["name"], json.loads(entry["function"]["arguments"])))
            if truncated is not None and not message["content"]:
                message["content"] = _partial_text(text)
            messages = [message]
        if len({call.id for call in result_calls}) != len(result_calls) or any(not c.id or not isinstance(c.arguments, dict) for c in result_calls):
            raise ValueError("invalid or duplicate tool call identifiers")
        return Turn(messages, result_calls, text)
