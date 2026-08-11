from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.core.config import LLMConfig
from backend.core.json_compat import extract_json_dict, json_dict_candidates

# Action kinds a member/diamond can emit.
ACTION_KINDS = (
    "tool",        # call an MCP tool: {server, tool, args}
    "bash",        # run a command in the sandbox: {command}
    "memory",      # search memory: {query}
    "tool_search", # search the tool catalog: {query}
    "report",      # difficulty report to Diamond: {progress,difficulty,steps,directions,knowledge}
    "intent",      # declare a new exploration intent: {from, description}
    "conclude",    # conclude the assigned intent: {description}
    "flag",        # claim a flag: {flag, description, from}
    "done",        # give up / nothing more to do: {reason}
)


@dataclass(slots=True)
class MemberAction:
    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    thought: str = ""

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> MemberAction:
        if not isinstance(obj, dict):
            raise ValueError(f"model action must be a JSON object, got {type(obj).__name__}")
        normalized = _normalize_action_object(obj)
        raw_kind = normalized.get("action") or normalized.get("kind")
        kind = _normalize_action_kind(raw_kind)
        if kind not in ACTION_KINDS:
            raise ValueError(f"invalid action kind: {kind!r}")
        args = {
            k: v
            for k, v in normalized.items()
            if k not in ("action", "kind", "thought", "reasoning", "rationale")
        }
        if kind == "bash":
            for alias in ("cmd", "shell", "script", "code"):
                if "command" not in args and alias in args:
                    args["command"] = args.pop(alias)
            if isinstance(args.get("command"), list):
                args["command"] = " && ".join(str(part) for part in args["command"])
        if kind == "tool":
            for alias in ("name", "tool_name", "function"):
                if "tool" not in args and alias in args and isinstance(args[alias], str):
                    args["tool"] = args.pop(alias)
            for alias in ("mcp", "mcp_server", "server_name"):
                if "server" not in args and alias in args:
                    args["server"] = args.pop(alias)
            tool_args = args.get("args", {})
            if isinstance(tool_args, str):
                try:
                    tool_args = extract_json_dict(tool_args)
                except ValueError:
                    tool_args = {}
            args["args"] = tool_args if isinstance(tool_args, dict) else {}
        if kind in {"memory", "tool_search"} and "query" not in args:
            for alias in ("search", "term", "prompt"):
                if alias in args:
                    args["query"] = args.pop(alias)
                    break
        if kind in {"intent", "conclude"} and "description" not in args:
            for alias in ("summary", "result", "content"):
                if alias in args:
                    args["description"] = args.pop(alias)
                    break
        if kind == "done" and "reason" not in args:
            for alias in ("message", "summary"):
                if alias in args:
                    args["reason"] = args.pop(alias)
                    break
        thought = normalized.get("thought", normalized.get("reasoning", normalized.get("rationale", "")))
        return cls(kind=kind, args=args, thought=thought if isinstance(thought, str) else "")


@dataclass(slots=True)
class ModelToolCall:
    name: str
    arguments: Any = field(default_factory=dict)
    call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            **({"id": self.call_id} if self.call_id else {}),
        }


@dataclass(slots=True)
class ModelReply:
    """Provider-neutral result used at the model/runtime boundary."""

    text: str = ""
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Opaque provider continuation data. The runtime keeps this alongside the
    # canonical tool call so reasoning/tool-use blocks can be replayed exactly
    # on the next provider request without leaking provider shapes into the
    # orchestration loop.
    continuation: dict[str, Any] = field(default_factory=dict)


_ACTION_KIND_ALIASES = {
    "shell": "bash",
    "terminal": "bash",
    "exec": "bash",
    "execute": "bash",
    "execute_command": "bash",
    "run_command": "bash",
    "mcp": "tool",
    "mcp_tool": "tool",
    "call_tool": "tool",
    "tool_call": "tool",
    "search_memory": "memory",
    "memory_search": "memory",
    "recall": "memory",
    "search_tools": "tool_search",
    "find_tool": "tool_search",
    "difficulty_report": "report",
    "status_report": "report",
    "new_intent": "intent",
    "create_intent": "intent",
    "finish_intent": "conclude",
    "conclusion": "conclude",
    "submit_flag": "flag",
    "found_flag": "flag",
    "finish": "done",
    "stop": "done",
    "final": "done",
    "final_answer": "done",
    "give_up": "done",
}
_ACTION_WRAPPERS = (
    "next_action",
    "decision",
    "response",
    "result",
    "output",
    "message",
    "content",
    "data",
)
_GENERIC_ACTION_TOOLS = {"action", "ipc_action", "member_action", "submit_action"}


def decode_member_action(*values: Any) -> MemberAction:
    """Decode the first semantically valid action across provider candidates."""

    errors: list[str] = []
    saw_object = False
    for value in values:
        if isinstance(value, ModelToolCall):
            sources: list[Any] = [_tool_call_action_source(value.name, value.arguments)]
        elif isinstance(value, ModelReply):
            if len(value.tool_calls) > 1:
                raise ValueError("exactly one native action tool call is allowed per decision")
            sources = [
                *(_tool_call_action_source(call.name, call.arguments) for call in value.tool_calls),
                value.text,
            ]
        else:
            sources = [value]
        for source in sources:
            for candidate in json_dict_candidates(source):
                saw_object = True
                try:
                    action = MemberAction.from_obj(candidate)
                    _validate_member_action(action)
                    return action
                except (TypeError, ValueError) as exc:
                    if len(errors) < 8:
                        errors.append(str(exc))
    if saw_object and errors:
        detail = "; ".join(dict.fromkeys(errors))
        raise ValueError(f"no valid JSON action found: {detail}")
    raise ValueError("no JSON action found in model output")


def _validate_member_action(action: MemberAction) -> None:
    """Validate execution-critical fields before an action reaches the loop."""

    if action.kind == "bash":
        command = action.args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("bash action requires a non-empty command string")
    elif action.kind == "tool":
        server = action.args.get("server")
        tool = action.args.get("tool")
        if not isinstance(server, str) or not server.strip():
            raise ValueError("tool action requires a non-empty server string")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool action requires a non-empty tool string")
        if not isinstance(action.args.get("args"), dict):
            raise ValueError("tool action args must be an object")
    elif action.kind == "flag":
        flag = action.args.get("flag")
        if not isinstance(flag, str) or not flag.strip():
            raise ValueError("flag action requires a non-empty flag string")


def _normalize_action_object(value: dict[str, Any]) -> dict[str, Any]:
    obj = {_normalize_protocol_key(key): item for key, item in value.items()}
    inherited_thought = _first_value(obj, "thought", "reasoning", "rationale")

    for _ in range(5):
        native = _native_tool_call_object(obj)
        if native is not None:
            obj = native
            if inherited_thought and not _first_value(obj, "thought", "reasoning", "rationale"):
                obj["thought"] = inherited_thought
            continue

        action_value = obj.get("action")
        if isinstance(action_value, dict):
            inner = {_normalize_protocol_key(key): item for key, item in action_value.items()}
            nested_kind = _first_value(inner, "action", "kind", "action_type", "type", "name")
            obj = {**inner, **{key: item for key, item in obj.items() if key != "action"}}
            if nested_kind is not None:
                obj["action"] = nested_kind
            continue

        if _first_value(obj, "action", "kind", "action_type") is None:
            unwrapped = False
            for key in _ACTION_WRAPPERS:
                nested = obj.get(key)
                if isinstance(nested, dict):
                    outer = {
                        outer_key: item
                        for outer_key, item in obj.items()
                        if outer_key not in _ACTION_WRAPPERS
                    }
                    obj = {
                        **{_normalize_protocol_key(child_key): item for child_key, item in nested.items()},
                        **outer,
                    }
                    unwrapped = True
                    break
            if unwrapped:
                continue
        break

    raw_kind = _first_value(obj, "action", "kind", "action_type")
    if raw_kind is None:
        type_value = obj.get("type")
        if _normalize_action_kind(type_value) in ACTION_KINDS:
            raw_kind = type_value
        else:
            for key, nested in list(obj.items()):
                if _normalize_action_kind(key) not in ACTION_KINDS or not isinstance(nested, dict):
                    continue
                obj = {
                    **{_normalize_protocol_key(child_key): item for child_key, item in nested.items()},
                    **{outer_key: item for outer_key, item in obj.items() if outer_key != key},
                }
                raw_kind = key
                break
    if raw_kind is None:
        if any(key in obj for key in ("command", "cmd", "shell", "script")):
            raw_kind = "bash"
        elif "server" in obj and any(key in obj for key in ("tool", "tool_name", "name")):
            raw_kind = "tool"

    kind = _normalize_action_kind(raw_kind)
    obj["action"] = kind
    obj.pop("kind", None)
    obj.pop("action_type", None)
    if obj.get("type") == raw_kind or _normalize_action_kind(obj.get("type")) == kind:
        obj.pop("type", None)

    containers = ("arguments", "parameters", "input", "payload")
    nested_values = [
        (key, _dict_value(obj.get(key)), _raw_dict_value(obj.get(key)))
        for key in containers
        if key in obj
    ]
    direct = {key: item for key, item in obj.items() if key not in containers}
    raw_args = _dict_value(direct.get("args")) if "args" in direct else None

    if kind == "tool":
        if raw_args and not any(key in direct for key in ("server", "tool", "tool_name", "name")):
            if any(key in raw_args for key in ("server", "tool", "tool_name", "name")):
                direct.pop("args", None)
                direct = {**raw_args, **direct}
        has_explicit_target = "server" in direct and any(
            key in direct for key in ("tool", "tool_name", "name")
        )
        for _, nested, raw_nested in nested_values:
            if not nested:
                continue
            if has_explicit_target:
                if "args" not in direct:
                    direct["args"] = raw_nested or nested
            elif any(key in nested for key in ("server", "tool", "tool_name", "name")):
                direct = {**nested, **direct}
                has_explicit_target = "server" in direct and any(
                    key in direct for key in ("tool", "tool_name", "name")
                )
            elif "args" not in direct:
                direct["args"] = raw_nested or nested
    else:
        merged: dict[str, Any] = {}
        if raw_args:
            merged.update(raw_args)
            direct.pop("args", None)
        for _, nested, _ in nested_values:
            if nested:
                merged.update(nested)
        direct = {**merged, **direct}
    return direct


def _native_tool_call_object(obj: dict[str, Any]) -> dict[str, Any] | None:
    calls = obj.get("tool_calls")
    if isinstance(calls, list) and calls:
        first = calls[0]
        if isinstance(first, dict):
            return {_normalize_protocol_key(key): item for key, item in first.items()}
    call = obj.get("tool_call") or obj.get("function_call")
    if isinstance(call, dict):
        return {_normalize_protocol_key(key): item for key, item in call.items()}
    function = obj.get("function")
    if isinstance(function, dict):
        name = function.get("name") or obj.get("name")
        arguments = function.get("arguments", function.get("input", {}))
        return _tool_call_action_source(name, arguments)
    item_type = str(obj.get("type", "")).strip().lower()
    if item_type in {"function_call", "tool_call", "tool_use"} and isinstance(obj.get("name"), str):
        arguments = obj.get("arguments", obj.get("input", obj.get("args", {})))
        return _tool_call_action_source(obj["name"], arguments)
    return None


def _tool_call_action_source(name: Any, arguments: Any) -> dict[str, Any]:
    normalized_name = _normalize_protocol_key(name)
    parsed_arguments = _dict_value(arguments) or {}
    if normalized_name in _GENERIC_ACTION_TOOLS:
        return parsed_arguments
    kind = _normalize_action_kind(normalized_name)
    if kind in ACTION_KINDS:
        return {"action": kind, **parsed_arguments}
    if _first_value(parsed_arguments, "action", "kind", "action_type") is not None:
        return parsed_arguments
    return {"action": normalized_name, **parsed_arguments}


def _dict_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return {_normalize_protocol_key(key): item for key, item in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            parsed = extract_json_dict(value)
        except ValueError:
            return None
        return {_normalize_protocol_key(key): item for key, item in parsed.items()}
    return None


def _raw_dict_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return extract_json_dict(value)
        except ValueError:
            return None
    return None


def _normalize_protocol_key(value: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().lower())


def _normalize_action_kind(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = _normalize_protocol_key(value)
    return _ACTION_KIND_ALIASES.get(normalized, normalized)


def _first_value(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


class DecisionOutputError(ValueError):
    """The model answered, but did not produce a usable Member action."""

    def __init__(self, attempts: list[dict[str, Any]]):
        self.attempts = attempts
        detail = attempts[-1].get("error", "invalid model output") if attempts else "invalid model output"
        response = attempts[-1].get("response", {}) if attempts else {}
        if response.get("finish_reason"):
            detail += (
                f" (finish_reason={response['finish_reason']}, "
                f"content_length={response.get('content_length', 'unknown')})"
            )
        super().__init__(
            f"model did not return a valid JSON action after {len(attempts)} attempt(s): {detail}"
        )


class ProviderError(RuntimeError):
    """A classified failure while calling an LLM provider."""

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        retry_after: float | None = None,
        response: Any = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
        self.response = response


class RetryableProviderError(ProviderError):
    """A transient provider failure that may succeed when attempted later."""

    retryable = True


class NonRetryableProviderError(ProviderError):
    """A provider failure that should not be replayed without changing input/config."""

    retryable = False


_PROVIDER_MAX_ATTEMPTS = 3
_PROVIDER_BACKOFF_BASE_SECONDS = 0.5
_PROVIDER_BACKOFF_CAP_SECONDS = 4.0
_PROVIDER_RETRY_AFTER_CAP_SECONDS = 30.0


class BaseAdapter:
    def __init__(self, config: LLMConfig, name: str = "agent"):
        self.config = config
        self.name = name

    def health(self) -> dict:
        raise NotImplementedError

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """Return a provider-neutral reply without breaking legacy chat callers."""

        return ModelReply(
            text=self.chat(
                messages,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

    def decide(self, context: dict) -> MemberAction:
        raise NotImplementedError


def health_check(config: LLMConfig) -> dict:
    """Validate a single LLM endpoint config."""
    return make_adapter(config).health()


def make_adapter(config: LLMConfig, name: str = "agent", script: list | None = None) -> BaseAdapter:
    fmt = config.api_format
    if fmt == "mock" or script is not None:
        from backend.members.mock_adapter import MockAdapter

        return MockAdapter(config, name=name, script=script)
    if fmt in ("openai", "deepseek"):
        return OpenAICompatibleAdapter(config, name=name)
    if fmt in ("anthropic", "claudecode"):
        return ClaudeAdapter(config, name=name)
    if fmt == "pi":
        return PiAdapter(config, name=name)
    raise ValueError(f"unknown api_format: {fmt}")

_SYSTEM_PROMPT = (
    "You are an expert CTF solver agent. Respond with EXACTLY ONE JSON object describing "
    "your next action and nothing else. Schema: "
    '{"thought": "...", "action": "tool|bash|memory|tool_search|report|intent|conclude|flag|done", ...}. '
    "For bash actions, include a non-empty `command` string exactly; do not use `cmd`, `shell`, "
    "or prose-only bash actions. For tool actions, include non-empty `server` and `tool` strings plus "
    "an `args` object when arguments are needed. "
    "Keep thought under 240 characters and a bash command under 1500 characters. Split long investigations "
    "across multiple actions instead of returning an oversized command. "
    "You are working inside a short exploration task: each run has only some actions, "
    "so produce a clear result quickly. End with conclude for a confirmed fact, flag for a real flag, "
    "intent for a concrete next direction, or report when blocked; do not silently spin. "
    "At the start of each CTF round, check likely flag sources in this order: first try reading /flag, "
    "then inspect challenge environment variables(FLAG or check ENV), then continue with challenge-specific methods. "
    "If recent_observations already contain the result of either probe, reuse it instead of running that probe again. "
    "Never claim a fake flag; use the flag action only for a confirmed real flag. "
    "Always inspect attachments and other provided materials first if they exist, because they may contain "
    "the real foothold or clue. If the current path is not moving, switch angle instead of repeating the same recon. "
    "If context attachment_true is false, no attachment was uploaded for the project; do not search for or read attachment files. "
    "Read the member_tool_inventory in your context before choosing tools; it summarizes installed CLI tools, "
    "Python libraries, MCP helpers, and when to use them. Check exposed tools or use tool_search for pyjail/sandbox "
    "helpers before spending many steps on manual subclass enumeration. "
    "When declaring a new intent, anchor its from field to the most relevant latest confirmed fact id rather than "
    "resetting to origin unless you are intentionally restarting from the root. "
    "Do not mention CVEs unless the current evidence really points to a component/version issue. "
    "Difficulty calibration must use exactly low, medium, high, or ex. Use low for source disclosure, direct "
    "attachment clues, standard exploit chains, single-surface tasks, or anything a single focused agent should "
    "likely finish soon. Use medium when evidence shows real branching uncertainty, two short tasks on the same "
    "intent produced no new fact, repeated action signatures, two distinct exploit classes, or two credible attack "
    "surfaces. Use high when three short tasks on the same intent produced no new fact, several distinct exploit "
    "classes have truly been tried, or three credible attack surfaces exist. Use ex only for extreme combined "
    "stuckness or four-plus credible surfaces/classes. Do not count the same exploit class repeated with tiny "
    "payload variations as distinct evidence. If two consecutive evaluations find the same difficulty level, do "
    "not report again unless there is new evidence or a changed direction. "
    "Use bash to run sandbox commands, tool to call an MCP tool {server,tool,args}, memory to recall "
    "past experience {query}, report to escalate difficulty to Diamond "
    "{progress,difficulty,steps,directions,knowledge}, conclude to record a confirmed fact for your "
    "assigned intent {description}, flag when you have the real flag {flag,description,from}."
)


_ACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": list(ACTION_KINDS)},
    },
    "required": ["action"],
    # Action-specific fields intentionally remain open. Tool arguments and CTF
    # reports are dynamic and cannot be represented by one strict schema.
    "additionalProperties": True,
}


@dataclass(slots=True)
class _OpenAIProfile:
    """Negotiated capabilities for one OpenAI-compatible request shape."""

    surface: str
    structured_mode: str
    token_parameter: str | None
    allow_temperature: bool
    allow_reasoning: bool
    allow_thinking: bool
    allow_tools: bool

    def signature(self) -> tuple[Any, ...]:
        return (
            self.surface,
            self.structured_mode,
            self.token_parameter,
            self.allow_temperature,
            self.allow_reasoning,
            self.allow_thinking,
            self.allow_tools,
        )


class OpenAICompatibleAdapter(BaseAdapter):

    def __init__(self, config: LLMConfig, name: str = "agent"):
        super().__init__(config, name=name)
        self._last_response_meta: dict[str, Any] = {}
        self._surface_cache: str | None = None
        self._profile_cache: dict[tuple[Any, ...], _OpenAIProfile] = {}

    def _endpoint(self, surface: str) -> str:
        return _openai_endpoint(self.config.base_url, surface)

    def health(self) -> dict:
        try:
            self._request_compatible(
                [{"role": "user", "content": "Reply with OK."}],
                system_prompt="",
                temperature=None,
                max_tokens=16,
                structured=False,
                reasoning_effort=("none" if _is_reasoning_model(self.config.model) else None),
                thinking=None,
                timeout=15,
            )
            return {
                "ok": True,
                "status": self._last_response_meta.get("http_status", 200),
                "format": self.config.api_format,
                "surface": self._last_response_meta.get("surface"),
            }
        except ProviderError as exc:
            return {
                "ok": False,
                "status": exc.status_code or 0,
                "error": _http_error_summary(exc),
                "format": self.config.api_format,
                "retryable": exc.retryable,
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": self.config.api_format}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        deepseek = self.config.api_format == "deepseek"
        reasoning_model = _is_reasoning_model(self.config.model)
        reasoning_effort = self._reasoning_effort(decision=True)
        reply = self._request_compatible(
            messages,
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0 if deepseek else (None if reasoning_model else 0.4),
            max_tokens=4096 if deepseek or reasoning_model else None,
            structured=True,
            reasoning_effort=reasoning_effort,
            thinking="disabled" if deepseek else None,
        )
        attempts: list[dict[str, Any]] = []
        try:
            return decode_member_action(reply)
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(reply, exc, self._last_response_meta))
            first_error = str(exc)

        repair_message = (
            "Repair the previous response. It may have been truncated. Return exactly one shorter JSON object "
            "matching the action schema, with no Markdown or commentary. Keep thought under 240 characters and "
            "any bash command under 1500 characters. The validator error was: "
            + _clip_text(first_error, 1000)
        )
        repaired = self._request_compatible(
            [
                *messages,
                {"role": "assistant", "content": _reply_preview(reply, 6000)},
                {"role": "user", "content": repair_message},
            ],
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0 if not reasoning_model else None,
            max_tokens=4096 if deepseek or reasoning_model else 1024,
            structured=True,
            reasoning_effort=reasoning_effort,
            thinking="disabled" if deepseek else None,
        )
        try:
            return decode_member_action(repaired)
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(repaired, exc, self._last_response_meta))
            raise DecisionOutputError(attempts) from exc

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        return self.complete(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ).text

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        return self._request_compatible(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            structured=False,
            reasoning_effort=self._reasoning_effort(decision=False),
            thinking=None,
            tools=tools,
        )

    def _reasoning_effort(self, *, decision: bool) -> str | None:
        configured = self.config.reasoning_effort
        if configured != "auto":
            return configured
        if decision and _is_reasoning_model(self.config.model):
            # Member decisions are short routing actions. Avoid spending the
            # output budget on hidden reasoning unless the operator opts in.
            return "none"
        return None

    def _surface_order(self) -> list[str]:
        configured = self.config.api_surface
        if configured != "auto":
            return [configured]
        if self.config.api_format == "deepseek":
            return ["chat_completions"]
        preferred = self._surface_cache
        if preferred is None:
            preferred = (
                "responses"
                if _prefers_responses(self.config.base_url, self.config.model)
                else "chat_completions"
            )
        alternate = "chat_completions" if preferred == "responses" else "responses"
        return [preferred, alternate]

    def _default_profile(
        self,
        surface: str,
        *,
        structured: bool,
        reasoning_effort: str | None,
        thinking: str | None,
        tools: list[dict[str, Any]] | None = None,
    ) -> _OpenAIProfile:
        reasoning_model = _is_reasoning_model(self.config.model)
        native_or_reasoning = _is_native_openai(self.config.base_url) or reasoning_model
        if not structured:
            structured_mode = "none"
        elif self.config.api_format == "deepseek":
            structured_mode = "json_object"
        elif native_or_reasoning:
            structured_mode = "json_schema"
        else:
            structured_mode = "json_object"

        if surface == "responses":
            token_parameter = "max_output_tokens"
        elif self.config.api_format == "deepseek" or not native_or_reasoning:
            token_parameter = "max_tokens"
        else:
            token_parameter = "max_completion_tokens"

        return _OpenAIProfile(
            surface=surface,
            structured_mode=structured_mode,
            token_parameter=token_parameter,
            allow_temperature=not reasoning_model,
            allow_reasoning=bool(reasoning_effort) and self.config.api_format != "deepseek",
            allow_thinking=bool(thinking) and surface == "chat_completions",
            allow_tools=bool(tools),
        )

    def _request_compatible(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str,
        temperature: float | None,
        max_tokens: int | None,
        structured: bool,
        reasoning_effort: str | None,
        thinking: str | None,
        tools: list[dict[str, Any]] | None = None,
        timeout: int = 120,
    ) -> ModelReply:
        request_messages = list(messages)
        if system_prompt:
            request_messages.insert(0, {"role": "system", "content": system_prompt})
        last_error: ProviderError | None = None
        surfaces = self._surface_order()

        for surface in surfaces:
            cache_key = (
                surface,
                structured,
                reasoning_effort or "",
                thinking or "",
                temperature is not None,
                max_tokens is not None,
                bool(tools),
            )
            profile = self._profile_cache.get(cache_key) or self._default_profile(
                surface,
                structured=structured,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
                tools=tools,
            )
            seen: set[tuple[Any, ...]] = set()

            for _ in range(8):
                signature = profile.signature()
                if signature in seen:
                    break
                seen.add(signature)
                request_body = _openai_request_body(
                    profile,
                    model=self.config.model or "gpt-4o",
                    messages=request_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    thinking=thinking,
                    tools=tools,
                )
                resp = _post_with_retries(
                    self._endpoint(surface),
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json_body=request_body,
                    timeout=timeout,
                    provider=self.config.api_format,
                )
                if _response_status(resp) >= 400:
                    last_error = _provider_error_from_response(resp, self.config.api_format)
                    issue, error_text = _compatibility_issue(resp, profile)
                    if issue == "endpoint":
                        break
                    degraded = _degrade_profile(profile, issue, error_text)
                    if degraded is None or degraded.signature() in seen:
                        raise last_error
                    profile = degraded
                    continue

                payload = _provider_response_json(resp, self.config.api_format)
                try:
                    reply = _openai_response(payload, surface)
                except (TypeError, ValueError) as exc:
                    raise NonRetryableProviderError(
                        f"{self.config.api_format} returned an invalid response envelope: {exc}",
                        provider=self.config.api_format,
                        status_code=_response_status(resp),
                        response=resp,
                    ) from exc
                reply.metadata.update(
                    {
                        "surface": surface,
                        "http_status": getattr(resp, "status_code", 200),
                    }
                )
                self._last_response_meta = reply.metadata
                self._surface_cache = surface
                self._profile_cache[cache_key] = profile
                return reply

            if self.config.api_surface != "auto":
                break

        if last_error is not None:
            raise last_error
        raise RuntimeError("no compatible OpenAI API request profile was available")


def _openai_request_body(
    profile: _OpenAIProfile,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None,
    max_tokens: int | None,
    reasoning_effort: str | None,
    thinking: str | None,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model}
    if profile.surface == "responses":
        body["input"] = _responses_request_input(messages)
    else:
        body["messages"] = _chat_request_messages(messages)

    if tools and profile.allow_tools:
        body["tools"] = _openai_function_tools(tools, surface=profile.surface)

    if temperature is not None and profile.allow_temperature:
        body["temperature"] = temperature
    if max_tokens is not None and profile.token_parameter is not None:
        body[profile.token_parameter] = max_tokens

    if profile.structured_mode == "json_schema":
        if profile.surface == "responses":
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "ipc_member_action",
                    "strict": False,
                    "schema": _ACTION_JSON_SCHEMA,
                }
            }
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ipc_member_action",
                    "strict": False,
                    "schema": _ACTION_JSON_SCHEMA,
                },
            }
    elif profile.structured_mode == "json_object":
        if profile.surface == "responses":
            body["text"] = {"format": {"type": "json_object"}}
        else:
            body["response_format"] = {"type": "json_object"}

    if reasoning_effort and profile.allow_reasoning:
        if profile.surface == "responses":
            body["reasoning"] = {"effort": reasoning_effort}
        else:
            body["reasoning_effort"] = reasoning_effort
    if thinking and profile.allow_thinking:
        body["thinking"] = {"type": thinking}
    return body


def _openai_function_tools(
    tools: list[dict[str, Any]],
    *,
    surface: str,
) -> list[dict[str, Any]]:
    """Translate IPC's provider-neutral function catalogue to an API shape."""

    translated: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        definition = {
            "name": name.strip(),
            "description": str(item.get("description") or ""),
            "parameters": (
                item["parameters"]
                if isinstance(item.get("parameters"), dict)
                else {"type": "object", "properties": {}}
            ),
            # Best-effort mode is intentional: older compatible gateways tend
            # to reject strict schemas, while IPC validates before dispatch.
            "strict": False,
        }
        if surface == "responses":
            translated.append({"type": "function", **definition})
        else:
            translated.append({"type": "function", "function": definition})
    return translated


def _tool_arguments_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps({"value": str(value)}, ensure_ascii=False, separators=(",", ":"))


def _canonical_tool_calls(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    calls: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, ModelToolCall):
            item = item.as_dict()
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        source = function if isinstance(function, dict) else item
        name = source.get("name") or source.get("tool")
        if not isinstance(name, str) or not name.strip():
            continue
        call_id = item.get("call_id") or item.get("id")
        calls.append(
            {
                "name": name.strip(),
                "arguments": source.get("arguments", source.get("input", source.get("args", {}))),
                **({"id": str(call_id)} if call_id is not None else {}),
            }
        )
    return calls


def _chat_request_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render canonical runtime messages for Chat Completions."""

    rendered: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        if role == "tool":
            call_id = message.get("tool_call_id") or message.get("call_id")
            if call_id is None:
                # A malformed gateway call without an id cannot be linked
                # natively; retain the evidence as an ordinary user turn.
                rendered.append({"role": "user", "content": str(message.get("content") or "")})
                continue
            rendered.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call_id),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        calls = _canonical_tool_calls(message.get("tool_calls"))
        if role == "assistant" and calls:
            rendered.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or None,
                    "tool_calls": [
                        {
                            "id": call.get("id") or f"ipc_call_{len(rendered) + index}",
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": _tool_arguments_text(call.get("arguments")),
                            },
                        }
                        for index, call in enumerate(calls)
                    ],
                }
            )
            continue
        rendered.append({"role": role, "content": message.get("content", "")})
    return rendered


def _responses_request_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render canonical runtime messages for the Responses API.

    Native output items are replayed when present. This preserves encrypted or
    summarized reasoning blocks required by reasoning models during tool loops.
    """

    rendered: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        continuation = message.get("continuation")
        if (
            isinstance(continuation, dict)
            and continuation.get("provider") == "openai_responses"
            and isinstance(continuation.get("items"), list)
        ):
            rendered.extend(item for item in continuation["items"] if isinstance(item, dict))
            continue
        role = str(message.get("role") or "user")
        if role == "tool":
            call_id = message.get("tool_call_id") or message.get("call_id")
            if call_id is None:
                rendered.append({"role": "user", "content": str(message.get("content") or "")})
                continue
            rendered.append(
                {
                    "type": "function_call_output",
                    "call_id": str(call_id),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        calls = _canonical_tool_calls(message.get("tool_calls"))
        if role == "assistant" and calls:
            content = message.get("content")
            if content:
                rendered.append({"role": "assistant", "content": content})
            for index, call in enumerate(calls):
                rendered.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or f"ipc_call_{len(rendered) + index}",
                        "name": call["name"],
                        "arguments": _tool_arguments_text(call.get("arguments")),
                    }
                )
            continue
        rendered.append({"role": role, "content": message.get("content", "")})
    return rendered


def _openai_response(payload: Any, surface: str) -> ModelReply:
    if not isinstance(payload, dict):
        raise ValueError(f"LLM response must be a JSON object, got {type(payload).__name__}")
    # Several compatibility gateways expose Responses at a custom path but
    # still return a Chat-shaped payload. Detect the actual wire shape.
    if isinstance(payload.get("choices"), list):
        return _chat_model_reply(payload)
    if surface == "responses" or isinstance(payload.get("output"), list):
        return _responses_model_reply(payload)
    raise ValueError("LLM response contains neither choices nor output items")


def _chat_model_reply(payload: dict[str, Any]) -> ModelReply:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("Chat Completions response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    text = _content_text(content)
    if not text:
        text = _content_text(choice.get("text"))
    if not text:
        text = _content_text(message.get("refusal"))
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    reasoning_tokens = details.get("reasoning_tokens")
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    elif not isinstance(raw_calls, list):
        raw_calls = []
    legacy_call = message.get("function_call")
    if isinstance(legacy_call, dict):
        raw_calls = [*raw_calls, legacy_call]
    raw_calls.extend(_content_tool_calls(content))
    return ModelReply(
        text=text,
        tool_calls=_dedupe_model_tool_calls(
            [call for item in raw_calls if (call := _model_tool_call(item)) is not None]
        ),
        metadata={
            "finish_reason": choice.get("finish_reason"),
            "content_type": type(content).__name__,
            "content_length": len(text),
            "reasoning_present": bool(
                message.get("reasoning_content")
                or message.get("reasoning")
                or (isinstance(reasoning_tokens, (int, float)) and reasoning_tokens > 0)
            ),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": reasoning_tokens,
        },
    )


def _responses_model_reply(payload: dict[str, Any]) -> ModelReply:
    output = payload.get("output") or []
    if not isinstance(output, list):
        output = []
    parts: list[str] = []
    tool_calls: list[ModelToolCall] = []
    reasoning_present = False
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            reasoning_present = True
            continue
        if item_type in {"function_call", "tool_call", "tool_use"}:
            call = _model_tool_call(item)
            if call is not None:
                tool_calls.append(call)
            continue
        if item_type in {"output_text", "text"}:
            value = _content_text(item)
            if value:
                parts.append(value)
            continue
        if item_type == "message" or "content" in item:
            item_content = item.get("content")
            tool_calls.extend(_content_tool_calls(item_content))
            value = _content_text(item_content)
            if not value:
                value = _content_text(item.get("refusal"))
            if value:
                parts.append(value)
    top_level_text = payload.get("output_text")
    text = top_level_text if isinstance(top_level_text, str) and top_level_text else "\n".join(parts)

    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("output_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    reasoning_tokens = details.get("reasoning_tokens")
    incomplete = payload.get("incomplete_details") or {}
    if not isinstance(incomplete, dict):
        incomplete = {}
    status = payload.get("status")
    finish_reason = incomplete.get("reason") if status == "incomplete" else status
    return ModelReply(
        text=text,
        tool_calls=_dedupe_model_tool_calls(tool_calls),
        metadata={
            "finish_reason": finish_reason,
            "content_type": type(output).__name__,
            "content_length": len(text),
            "reasoning_present": bool(
                reasoning_present
                or (isinstance(reasoning_tokens, (int, float)) and reasoning_tokens > 0)
            ),
            "completion_tokens": usage.get("output_tokens"),
            "reasoning_tokens": reasoning_tokens,
        },
        continuation={
            "provider": "openai_responses",
            "items": [item for item in output if isinstance(item, dict)],
        },
    )


def _model_tool_call(value: Any) -> ModelToolCall | None:
    if not isinstance(value, dict):
        return None
    function = value.get("function")
    source = function if isinstance(function, dict) else value
    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = source.get("arguments", source.get("input", source.get("args", {})))
    call_id = value.get("call_id") or value.get("id")
    return ModelToolCall(
        name=name.strip(),
        arguments={} if arguments is None else arguments,
        call_id=str(call_id) if call_id is not None else None,
    )


def _content_tool_calls(content: Any) -> list[dict[str, Any]]:
    blocks = content if isinstance(content, list) else [content]
    calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        nested = block.get("tool_call") or block.get("function_call")
        if isinstance(nested, dict):
            calls.append(nested)
            continue
        block_type = str(block.get("type", "")).strip().lower()
        if block_type in {"function", "function_call", "tool_call", "tool_use"}:
            calls.append(block)
            continue
        if isinstance(block.get("function"), dict):
            calls.append(block)
    return calls


def _dedupe_model_tool_calls(calls: list[ModelToolCall]) -> list[ModelToolCall]:
    unique: list[ModelToolCall] = []
    seen: set[str] = set()
    for call in calls:
        if call.call_id:
            fingerprint = f"id:{call.call_id}"
        else:
            try:
                arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                arguments = repr(call.arguments)
            fingerprint = f"call:{call.name}:{arguments}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(call)
    return unique


def _compatibility_issue(response: Any, profile: _OpenAIProfile) -> tuple[str | None, str]:
    status = getattr(response, "status_code", 0) or 0
    if status not in {400, 404, 405, 415, 422}:
        return None, ""
    error_text = _response_error_text(response)
    if status in {404, 405}:
        return "endpoint", error_text

    parameter_problem = any(
        marker in error_text
        for marker in (
            "unknown parameter",
            "unsupported parameter",
            "not supported",
            "unrecognized",
            "extra inputs",
            "extra_forbidden",
            "not permitted",
            "invalid parameter",
        )
    )
    if profile.allow_tools and parameter_problem and any(
        marker in error_text
        for marker in ("tools", "tool_choice", "function calling", "function_call")
    ):
        return "tools", error_text
    endpoint_name = "responses" if profile.surface == "responses" else "chat/completions"
    if endpoint_name in error_text and any(
        marker in error_text for marker in ("unknown endpoint", "unsupported endpoint", "does not support")
    ):
        return "endpoint", error_text
    if profile.structured_mode == "json_schema" and "json_schema" in error_text:
        return "structured_schema", error_text
    if profile.structured_mode != "none" and (
        "response_format" in error_text
        or "text.format" in error_text
        or ("json_object" in error_text and parameter_problem)
    ):
        return "structured", error_text
    if profile.token_parameter and profile.token_parameter.lower() in error_text:
        return "token", error_text
    if profile.allow_temperature and "temperature" in error_text and parameter_problem:
        return "temperature", error_text
    if profile.allow_reasoning and (
        "reasoning_effort" in error_text or ("reasoning" in error_text and parameter_problem)
    ):
        return "reasoning", error_text
    if profile.allow_thinking and "thinking" in error_text and parameter_problem:
        return "thinking", error_text
    return None, error_text


def _degrade_profile(
    profile: _OpenAIProfile,
    issue: str | None,
    error_text: str,
) -> _OpenAIProfile | None:
    if issue == "structured_schema" and profile.structured_mode == "json_schema":
        return replace(profile, structured_mode="json_object")
    if issue == "structured" and profile.structured_mode != "none":
        return replace(profile, structured_mode="none")
    if issue == "temperature" and profile.allow_temperature:
        return replace(profile, allow_temperature=False)
    if issue == "reasoning" and profile.allow_reasoning:
        return replace(profile, allow_reasoning=False)
    if issue == "thinking" and profile.allow_thinking:
        return replace(profile, allow_thinking=False)
    if issue == "tools" and profile.allow_tools:
        return replace(profile, allow_tools=False)
    if issue == "token" and profile.token_parameter:
        if (
            profile.token_parameter == "max_tokens"
            and "max_completion_tokens" in error_text
        ):
            return replace(profile, token_parameter="max_completion_tokens")
        if (
            profile.token_parameter == "max_completion_tokens"
            and "max_tokens" in error_text
        ):
            return replace(profile, token_parameter="max_tokens")
        return replace(profile, token_parameter=None)
    return None


def _response_error_text(response: Any) -> str:
    try:
        value = response.json()
        return json.dumps(value, ensure_ascii=False)[:4000].lower()
    except Exception:
        return str(getattr(response, "text", ""))[:4000].lower()


def _response_status(response: Any) -> int:
    value = getattr(response, "status_code", 200)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_retryable_status(status: int) -> bool:
    return status in {408, 429} or 500 <= status <= 599


def _retry_after_seconds(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw: Any = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        pass
    if raw is None:
        try:
            raw = next(
                (value for key, value in headers.items() if str(key).lower() == "retry-after"),
                None,
            )
        except (AttributeError, TypeError):
            return None
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(_PROVIDER_RETRY_AFTER_CAP_SECONDS, max(0.0, delay))


def _provider_error_from_response(response: Any, provider: str) -> ProviderError:
    status = _response_status(response)
    retry_after = _retry_after_seconds(response)
    error_type = RetryableProviderError if _is_retryable_status(status) else NonRetryableProviderError
    return error_type(
        f"{provider} endpoint returned HTTP {status}",
        provider=provider,
        status_code=status,
        retry_after=retry_after,
        response=response,
    )


def _sleep_before_provider_retry(failed_attempt: int, response: Any = None) -> None:
    retry_after = _retry_after_seconds(response) if response is not None else None
    if retry_after is not None:
        delay = retry_after
    else:
        ceiling = min(
            _PROVIDER_BACKOFF_CAP_SECONDS,
            _PROVIDER_BACKOFF_BASE_SECONDS * (2 ** max(0, failed_attempt - 1)),
        )
        delay = random.uniform(0.0, ceiling)
    time.sleep(delay)


def _post_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: int,
    provider: str,
) -> Any:
    """POST once for permanent failures and at most three times for transient ones."""

    import requests

    transient_exceptions = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )
    for attempt in range(1, _PROVIDER_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except transient_exceptions as exc:
            error = RetryableProviderError(
                f"{provider} request failed: {type(exc).__name__}",
                provider=provider,
            )
            if attempt >= _PROVIDER_MAX_ATTEMPTS:
                raise error from exc
            _sleep_before_provider_retry(attempt)
            continue
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is None:
                raise NonRetryableProviderError(
                    f"{provider} request failed: HTTPError",
                    provider=provider,
                ) from exc
            error = _provider_error_from_response(response, provider)
            if not error.retryable or attempt >= _PROVIDER_MAX_ATTEMPTS:
                raise error from exc
            _sleep_before_provider_retry(attempt, response)
            continue
        except requests.RequestException as exc:
            raise NonRetryableProviderError(
                f"{provider} request failed: {type(exc).__name__}",
                provider=provider,
            ) from exc

        status = _response_status(response)
        if not _is_retryable_status(status):
            return response
        error = _provider_error_from_response(response, provider)
        if attempt >= _PROVIDER_MAX_ATTEMPTS:
            raise error
        _sleep_before_provider_retry(attempt, response)

    raise RetryableProviderError(
        f"{provider} request exhausted its retry budget",
        provider=provider,
    )


def _provider_response_json(response: Any, provider: str) -> Any:
    try:
        return response.json()
    except Exception as exc:
        raise NonRetryableProviderError(
            f"{provider} returned an invalid JSON response envelope",
            provider=provider,
            status_code=_response_status(response),
            response=response,
        ) from exc


def _http_error_summary(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        status = exc.status_code or 0
    else:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", 0) or 0
    return f"LLM endpoint returned HTTP {status}" if status else type(exc).__name__


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    if name.startswith("gpt-"):
        version = name[4:].split("-", 1)[0]
        try:
            return int(version.split(".", 1)[0]) >= 5
        except ValueError:
            pass
    return len(name) > 1 and name[0] == "o" and name[1].isdigit()


def _is_native_openai(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".api.openai.com")


def _prefers_responses(base_url: str, model: str) -> bool:
    return _is_native_openai(base_url) or _is_reasoning_model(model)


def _openai_endpoint(base_url: str, surface: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    if _is_native_openai(base_url) and not path:
        path = "/v1"
    suffix = "/responses" if surface == "responses" else "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}{suffix}", parsed.query, ""))


def _anthropic_endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/messages"):
        target = path
    elif path.endswith("/v1"):
        target = f"{path}/messages"
    else:
        target = f"{path}/v1/messages"
    return urlunsplit((parsed.scheme, parsed.netloc, target, parsed.query, ""))


def _anthropic_function_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        translated.append(
            {
                "name": name.strip(),
                "description": str(item.get("description") or ""),
                "input_schema": (
                    item["parameters"]
                    if isinstance(item.get("parameters"), dict)
                    else {"type": "object", "properties": {}}
                ),
            }
        )
    return translated


def _anthropic_request_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        continuation = message.get("continuation")
        if (
            role == "assistant"
            and isinstance(continuation, dict)
            and continuation.get("provider") == "anthropic"
            and isinstance(continuation.get("content"), list)
        ):
            rendered.append({"role": "assistant", "content": continuation["content"]})
            continue
        if role == "tool":
            call_id = message.get("tool_call_id") or message.get("call_id")
            if call_id is None:
                rendered.append({"role": "user", "content": str(message.get("content") or "")})
                continue
            rendered.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(call_id),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        calls = _canonical_tool_calls(message.get("tool_calls"))
        if role == "assistant" and calls:
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for index, call in enumerate(calls):
                arguments = call.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = extract_json_dict(arguments)
                    except ValueError:
                        arguments = {"value": arguments}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"ipc_call_{len(rendered) + index}",
                        "name": call["name"],
                        "input": arguments,
                    }
                )
            rendered.append({"role": "assistant", "content": blocks})
            continue
        rendered.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": message.get("content", ""),
            }
        )
    return rendered


class ClaudeAdapter(BaseAdapter):

    def __init__(self, config: LLMConfig, name: str = "agent"):
        super().__init__(config, name=name)
        self._last_response_meta: dict[str, Any] = {}

    def _endpoint(self) -> str:
        return _anthropic_endpoint(self.config.base_url)

    def _headers(self) -> dict[str, str]:
        # Anthropic uses x-api-key. Keep Bearer too for compatible gateways
        # (including Claude Code proxy surfaces) that expect OpenAI-style auth.
        return {
            "x-api-key": self.config.api_key,
            "Authorization": f"Bearer {self.config.api_key}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def health(self) -> dict:
        try:
            resp = _post_with_retries(
                self._endpoint(),
                headers=self._headers(),
                json_body={"model": self.config.model or "claude-opus-4-8", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]},
                timeout=15,
                provider=self.config.api_format,
            )
            if _response_status(resp) >= 400:
                raise _provider_error_from_response(resp, self.config.api_format)
            return {
                "ok": True,
                "status": _response_status(resp),
                "format": self.config.api_format,
                "surface": "messages",
            }
        except ProviderError as exc:
            return {
                "ok": False,
                "status": exc.status_code or 0,
                "error": _http_error_summary(exc),
                "format": self.config.api_format,
                "retryable": exc.retryable,
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": self.config.api_format}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        reply = self.complete(
            messages,
            system_prompt=_SYSTEM_PROMPT,
            temperature=None,
            max_tokens=1024,
        )
        attempts: list[dict[str, Any]] = []
        try:
            return decode_member_action(reply)
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(reply, exc, self._last_response_meta))
            first_error = str(exc)

        repaired = self.complete(
            [
                *messages,
                {"role": "assistant", "content": _reply_preview(reply, 6000)},
                {
                    "role": "user",
                    "content": (
                        "Repair the previous response. Return exactly one concise JSON action object, "
                        "with no Markdown or commentary. The validator error was: "
                        + _clip_text(first_error, 1000)
                    ),
                },
            ],
            system_prompt=_SYSTEM_PROMPT,
            temperature=None,
            max_tokens=1024,
        )
        try:
            return decode_member_action(repaired)
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(repaired, exc, self._last_response_meta))
            raise DecisionOutputError(attempts) from exc

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        return self.complete(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        ).text

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        request_body: dict[str, Any] = {
            "model": self.config.model or "claude-opus-4-8",
            "max_tokens": max_tokens or 2048,
            "messages": _anthropic_request_messages(messages),
        }
        if temperature is not None:
            request_body["temperature"] = temperature
        if system_prompt:
            request_body["system"] = system_prompt
        if tools:
            native_tools = _anthropic_function_tools(tools)
            if native_tools:
                request_body["tools"] = native_tools
        resp = _post_with_retries(
            self._endpoint(),
            headers=self._headers(),
            json_body=request_body,
            timeout=120,
            provider=self.config.api_format,
        )
        if _response_status(resp) >= 400:
            raise _provider_error_from_response(resp, self.config.api_format)
        payload = _provider_response_json(resp, self.config.api_format)
        if not isinstance(payload, dict):
            raise NonRetryableProviderError(
                f"{self.config.api_format} returned a non-object response envelope",
                provider=self.config.api_format,
                status_code=_response_status(resp),
                response=resp,
            )
        content = payload.get("content")
        text = _content_text(content)
        metadata = {
            "finish_reason": payload.get("stop_reason"),
            "content_type": type(content).__name__,
            "content_length": len(text),
            "reasoning_present": (
                any(
                    isinstance(block, dict) and block.get("type") in {"thinking", "reasoning"}
                    for block in content
                )
                if isinstance(content, list)
                else False
            ),
            "completion_tokens": (
                payload.get("usage", {}).get("output_tokens")
                if isinstance(payload.get("usage"), dict)
                else None
            ),
            "surface": "messages",
            "http_status": _response_status(resp),
        }
        tool_calls: list[ModelToolCall] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {"tool_use", "tool_call"}:
                    continue
                call = _model_tool_call(block)
                if call is not None:
                    tool_calls.append(call)
        reply = ModelReply(
            text=text,
            tool_calls=_dedupe_model_tool_calls(tool_calls),
            metadata=metadata,
            continuation=(
                {"provider": "anthropic", "content": content}
                if isinstance(content, list)
                else {}
            ),
        )
        self._last_response_meta = metadata
        return reply


class PiAdapter(OpenAICompatibleAdapter):
    """Pi uses an OpenAI-compatible surface in this build."""

    def health(self) -> dict:
        result = super().health()
        result["format"] = "pi"
        return result


def _extract_json(text: Any) -> dict:
    if text is None:
        raise ValueError("empty model output: response content is null")
    if isinstance(text, str) and not text.strip():
        raise ValueError("empty model output")
    try:
        return extract_json_dict(
            text,
            preferred_keys=(
                "action",
                "kind",
                "action_type",
                "next_action",
                "decision",
                "tool_call",
                "tool_calls",
                "function_call",
            ),
        )
    except ValueError as exc:
        raise ValueError("no JSON action found in model output") from exc


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "output_text", "refusal"):
                    if isinstance(block.get(key), str):
                        parts.append(block[key])
                        break
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "output_text", "refusal"):
            if isinstance(content.get(key), str):
                return content[key]
    return str(content)


def _clip_text(value: Any, limit: int) -> str:
    text = _content_text(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _reply_preview(value: Any, limit: int) -> str:
    if isinstance(value, ModelReply):
        parts = [value.text] if value.text else []
        parts.extend(
            json.dumps(call.as_dict(), ensure_ascii=False, default=str)
            for call in value.tool_calls
        )
        text = "\n".join(parts)
    else:
        text = _content_text(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _decision_attempt(text: Any, error: Exception, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": f"{type(error).__name__}: {error}",
        "preview": _reply_preview(text, 800),
        "response": dict(metadata),
    }
