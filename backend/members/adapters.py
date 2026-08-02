from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from typing import Any

from backend.core.config import LLMConfig

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
        raw_kind = obj.get("action") or obj.get("kind")
        kind = raw_kind.strip() if isinstance(raw_kind, str) else raw_kind
        if kind not in ACTION_KINDS:
            raise ValueError(f"invalid action kind: {kind!r}")
        args = {k: v for k, v in obj.items() if k not in ("action", "kind", "thought")}
        if kind == "bash":
            for alias in ("cmd", "shell", "script"):
                if "command" not in args and alias in args:
                    args["command"] = args.pop(alias)
            if isinstance(args.get("command"), list):
                args["command"] = " && ".join(str(part) for part in args["command"])
        if kind == "tool":
            if "tool" not in args and "name" in args:
                args["tool"] = args.pop("name")
            if not isinstance(args.get("args"), dict):
                args["args"] = {}
        thought = obj.get("thought", "")
        return cls(kind=kind, args=args, thought=thought if isinstance(thought, str) else "")


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


class BaseAdapter:
    def __init__(self, config: LLMConfig, name: str = "agent"):
        self.config = config
        self.name = name

    def health(self) -> dict:
        raise NotImplementedError

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

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
    if fmt == "claudecode":
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


class OpenAICompatibleAdapter(BaseAdapter):

    def __init__(self, config: LLMConfig, name: str = "agent"):
        super().__init__(config, name=name)
        self._last_response_meta: dict[str, Any] = {}

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return f"{base}/chat/completions"

    def health(self) -> dict:
        import requests

        try:
            resp = requests.post(
                self._endpoint(),
                headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.config.model or "gpt-4o",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
                timeout=15,
            )
            return {"ok": resp.status_code < 500, "status": resp.status_code, "format": self.config.api_format}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": self.config.api_format}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        json_mode = self.config.api_format == "deepseek"
        text = self._request_chat(
            messages,
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0 if json_mode else 0.4,
            max_tokens=4096 if json_mode else None,
            json_mode=json_mode,
            thinking="disabled" if json_mode else None,
        )
        attempts: list[dict[str, Any]] = []
        try:
            return MemberAction.from_obj(_extract_json(text))
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(text, exc, self._last_response_meta))

        repair_message = (
            "Repair the previous response. It may have been truncated. Return exactly one shorter JSON object "
            "matching the action schema, with no Markdown or commentary. Keep thought under 240 characters and "
            "any bash command under 1500 characters. The previous invalid response was:\n"
            + _clip_text(text, 6000)
        )
        repaired = self._request_chat(
            [*messages, {"role": "user", "content": repair_message}],
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=4096 if json_mode else 1024,
            json_mode=json_mode,
            thinking="disabled" if json_mode else None,
        )
        try:
            return MemberAction.from_obj(_extract_json(repaired))
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(repaired, exc, self._last_response_meta))
            raise DecisionOutputError(attempts) from exc

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        return self._request_chat(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
            thinking=None,
        )

    def _request_chat(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
        thinking: str | None,
    ) -> str:
        import requests

        request_messages = list(messages)
        if system_prompt:
            request_messages.insert(0, {"role": "system", "content": system_prompt})
        request_body: dict[str, Any] = {
            "model": self.config.model or "gpt-4o",
            "messages": request_messages,
        }
        if temperature is not None:
            request_body["temperature"] = temperature
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        if json_mode:
            request_body["response_format"] = {"type": "json_object"}
        if thinking is not None:
            request_body["thinking"] = {"type": thinking}
        resp = requests.post(
            self._endpoint(),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            json=request_body,
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        choice = payload["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        text = _content_text(content)
        usage = payload.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        self._last_response_meta = {
            "finish_reason": choice.get("finish_reason"),
            "content_type": type(content).__name__,
            "content_length": len(text),
            "reasoning_present": bool(message.get("reasoning_content") or message.get("reasoning")),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
        }
        return text


class ClaudeAdapter(BaseAdapter):

    def __init__(self, config: LLMConfig, name: str = "agent"):
        super().__init__(config, name=name)
        self._last_response_meta: dict[str, Any] = {}

    def health(self) -> dict:
        import requests

        try:
            resp = requests.post(
                f"{self.config.base_url.rstrip('/')}/v1/messages",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={"model": self.config.model or "claude-opus-4-8", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=15,
            )
            return {"ok": resp.status_code < 500, "status": resp.status_code, "format": "claudecode"}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": "claudecode"}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        text = self.chat(
            messages,
            system_prompt=_SYSTEM_PROMPT,
            temperature=None,
            max_tokens=1024,
        )
        attempts: list[dict[str, Any]] = []
        try:
            return MemberAction.from_obj(_extract_json(text))
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(text, exc, self._last_response_meta))

        repaired = self.chat(
            [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Repair the previous response. Return exactly one concise JSON action object, "
                        "with no Markdown or commentary. Previous invalid response:\n" + _clip_text(text, 6000)
                    ),
                },
            ],
            system_prompt=_SYSTEM_PROMPT,
            temperature=None,
            max_tokens=1024,
        )
        try:
            return MemberAction.from_obj(_extract_json(repaired))
        except (TypeError, ValueError) as exc:
            attempts.append(_decision_attempt(repaired, exc, self._last_response_meta))
            raise DecisionOutputError(attempts) from exc

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        import requests

        request_body: dict[str, Any] = {
            "model": self.config.model or "claude-opus-4-8",
            "max_tokens": max_tokens or 2048,
            "messages": messages,
        }
        if temperature is not None:
            request_body["temperature"] = temperature
        if system_prompt:
            request_body["system"] = system_prompt
        resp = requests.post(
            f"{self.config.base_url.rstrip('/')}/v1/messages",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        content = payload.get("content")
        text = _content_text(content)
        self._last_response_meta = {
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
        }
        return text


class PiAdapter(OpenAICompatibleAdapter):
    """Pi uses an OpenAI-compatible surface in this build."""

    def health(self) -> dict:
        result = super().health()
        result["format"] = "pi"
        return result


def _extract_json(text: Any) -> dict:
    return _extract_json_value(text, depth=0)


def _extract_json_value(text: Any, *, depth: int) -> dict:
    if text is None:
        raise ValueError("empty model output: response content is null")
    if isinstance(text, dict):
        return text
    if isinstance(text, list):
        candidate = _select_action_dict(text)
        if candidate is not None:
            return candidate
        raise ValueError("model output JSON array contains no action object")
    if not isinstance(text, str):
        text = str(text)
    text = text.strip()
    if not text:
        raise ValueError("empty model output")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            candidate = _select_action_dict(obj)
            if candidate is not None:
                return candidate
        if isinstance(obj, str) and depth < 2:
            return _extract_json_value(obj, depth=depth + 1)
    except json.JSONDecodeError:
        pass

    candidates: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue

    literal_sources = [_strip_code_fence(text), *_balanced_object_slices(text)]
    for source in literal_sources:
        try:
            obj = ast.literal_eval(source)
        except (SyntaxError, ValueError):
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
        elif isinstance(obj, list):
            candidates.extend(item for item in obj if isinstance(item, dict))

    candidate = _select_action_dict(candidates)
    if candidate is not None:
        return candidate
    raise ValueError("no JSON action found in model output")


def _select_action_dict(values: list[Any]) -> dict[str, Any] | None:
    dictionaries = [value for value in values if isinstance(value, dict)]
    return next(
        (value for value in dictionaries if "action" in value or "kind" in value),
        dictionaries[0] if dictionaries else None,
    )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline >= 0:
            return stripped[first_newline + 1 : -3].strip()
    return stripped


def _balanced_object_slices(text: str):
    start: int | None = None
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
                start = None


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
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


def _clip_text(value: Any, limit: int) -> str:
    text = _content_text(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _decision_attempt(text: Any, error: Exception, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": f"{type(error).__name__}: {error}",
        "preview": _clip_text(text, 800),
        "response": dict(metadata),
    }
