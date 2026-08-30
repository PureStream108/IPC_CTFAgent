from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
    "Flag discipline: report the exact characters from the evidence and never beautify OCR or "
    "noisy output into a plausible-looking phrase. When a flag is assembled from fragments, every "
    "fragment must be included and the joined content must read as a coherent phrase after leet "
    "decoding (4=a, @=a, $=s, 0=o, 7=t). The flag must match prefix{...} with non-empty content "
    "and no nested braces; decoy flags printed by the challenge itself are not the answer. "
    "Never resubmit a flag the platform already rejected; re-derive a complete flag instead. "
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


def _system_prompt_for_context(context: dict[str, Any]) -> str:
    return _SYSTEM_PROMPT


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

    def signature(self) -> tuple[Any, ...]:
        return (
            self.surface,
            self.structured_mode,
            self.token_parameter,
            self.allow_temperature,
            self.allow_reasoning,
            self.allow_thinking,
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
        import requests

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
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            return {
                "ok": False,
                "status": getattr(response, "status_code", 0) or 0,
                "error": _http_error_summary(exc),
                "format": self.config.api_format,
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": self.config.api_format}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        deepseek = self.config.api_format == "deepseek"
        reasoning_model = _is_reasoning_model(self.config.model)
        reasoning_effort = self._reasoning_effort(decision=True)
        # Forced-reasoning models (e.g. kimi-for-coding) spend the output
        # budget on hidden reasoning before any content; 4096 was observed
        # being fully consumed by reasoning alone on large contexts, leaving
        # an empty response with finish_reason=length.
        decision_tokens = 16384 if reasoning_model else (4096 if deepseek else None)
        text = self._request_compatible(
            messages,
            system_prompt=_system_prompt_for_context(context),
            temperature=0.0 if deepseek else (None if reasoning_model else 0.4),
            max_tokens=decision_tokens,
            structured=True,
            reasoning_effort=reasoning_effort,
            thinking="disabled" if deepseek else None,
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
        repaired = self._request_compatible(
            [*messages, {"role": "user", "content": repair_message}],
            system_prompt=_system_prompt_for_context(context),
            temperature=0.0 if not reasoning_model else None,
            max_tokens=16384 if reasoning_model else (4096 if deepseek else 1024),
            structured=True,
            reasoning_effort=reasoning_effort,
            thinking="disabled" if deepseek else None,
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
        return self._request_compatible(
            messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            structured=False,
            reasoning_effort=self._reasoning_effort(decision=False),
            thinking=None,
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
    ) -> _OpenAIProfile:
        reasoning_model = _is_reasoning_model(self.config.model)
        # Reasoning-model names alone do not guarantee that a compatible
        # gateway implements OpenAI's strict JSON Schema wire shape.  When an
        # operator explicitly selects Chat Completions on a non-native
        # endpoint, prefer the broadly supported JSON Object mode (and its
        # matching token parameter).  Native OpenAI keeps the strict schema
        # profile used by Responses/Chat reasoning models.
        native_or_reasoning = _is_native_openai(self.config.base_url) or (
            reasoning_model and self.config.api_surface == "auto"
        )
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
        )

    def _request_compatible(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str,
        temperature: float | None,
        max_tokens: int | None,
        structured: bool,
        reasoning_effort: str | None,
        thinking: str | None,
        timeout: int = 120,
    ) -> str:
        import requests

        request_messages = list(messages)
        if system_prompt:
            request_messages.insert(0, {"role": "system", "content": system_prompt})
        last_error: requests.HTTPError | None = None
        surfaces = self._surface_order()

        for surface in surfaces:
            cache_key = (
                surface,
                structured,
                reasoning_effort or "",
                thinking or "",
                temperature is not None,
                max_tokens is not None,
            )
            profile = self._profile_cache.get(cache_key) or self._default_profile(
                surface,
                structured=structured,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
            )
            seen: set[tuple[Any, ...]] = set()

            for _ in range(6):
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
                )
                resp = requests.post(
                    self._endpoint(surface),
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                    timeout=timeout,
                )
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = exc
                    issue, error_text = _compatibility_issue(resp, profile)
                    if issue == "endpoint":
                        break
                    degraded = _degrade_profile(profile, issue, error_text)
                    if degraded is None or degraded.signature() in seen:
                        # Surface the gateway's own reason (rate limit,
                        # content filter, schema rejection...) — a bare
                        # "400 Bad Request" is undiagnosable from logs.
                        detail = (getattr(resp, "text", "") or "").strip()[:500]
                        raise requests.HTTPError(
                            f"{exc} | gateway response: {detail}",
                            response=resp,
                        ) from exc
                    profile = degraded
                    continue

                payload = resp.json()
                text, metadata = _openai_response_text(payload, surface)
                metadata.update(
                    {
                        "surface": surface,
                        "http_status": getattr(resp, "status_code", 200),
                    }
                )
                self._last_response_meta = metadata
                self._surface_cache = surface
                self._profile_cache[cache_key] = profile
                return text

            if self.config.api_surface != "auto":
                break

        if last_error is not None:
            raise last_error
        raise RuntimeError("no compatible OpenAI API request profile was available")


def _openai_request_body(
    profile: _OpenAIProfile,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float | None,
    max_tokens: int | None,
    reasoning_effort: str | None,
    thinking: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model}
    if profile.surface == "responses":
        body["input"] = messages
    else:
        body["messages"] = messages

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


def _openai_response_text(payload: Any, surface: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError(f"LLM response must be a JSON object, got {type(payload).__name__}")
    # Several compatibility gateways expose Responses at a custom path but
    # still return a Chat-shaped payload. Detect the actual wire shape.
    if isinstance(payload.get("choices"), list):
        return _chat_response_text(payload)
    if surface == "responses" or isinstance(payload.get("output"), list):
        return _responses_response_text(payload)
    raise ValueError("LLM response contains neither choices nor output items")


def _chat_response_text(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
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
        text = _content_text(message.get("refusal"))
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    reasoning_tokens = details.get("reasoning_tokens")
    return text, {
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
    }


def _responses_response_text(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    output = payload.get("output") or []
    if not isinstance(output, list):
        output = []
    parts: list[str] = []
    reasoning_present = False
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            reasoning_present = True
            continue
        if item_type in {"output_text", "text"}:
            value = _content_text(item)
            if value:
                parts.append(value)
            continue
        if item_type == "message" or "content" in item:
            value = _content_text(item.get("content"))
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
    return text, {
        "finish_reason": finish_reason,
        "content_type": type(output).__name__,
        "content_length": len(text),
        "reasoning_present": bool(
            reasoning_present
            or (isinstance(reasoning_tokens, (int, float)) and reasoning_tokens > 0)
        ),
        "completion_tokens": usage.get("output_tokens"),
        "reasoning_tokens": reasoning_tokens,
    }


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


def _http_error_summary(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", 0) or 0
    return f"LLM endpoint returned HTTP {status}" if status else type(exc).__name__


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    # Kimi For Coding always thinks; give it the reasoning-model budget.
    if "kimi-for-coding" in name or name.startswith("kimi-k"):
        return True
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
        import requests

        try:
            resp = requests.post(
                self._endpoint(),
                headers=self._headers(),
                json={"model": self.config.model or "claude-opus-4-8", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=15,
            )
            resp.raise_for_status()
            return {
                "ok": True,
                "status": resp.status_code,
                "format": self.config.api_format,
                "surface": "messages",
            }
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            return {
                "ok": False,
                "status": getattr(response, "status_code", 0) or 0,
                "error": _http_error_summary(exc),
                "format": self.config.api_format,
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc), "format": self.config.api_format}

    def decide(self, context: dict) -> MemberAction:
        messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        text = self.chat(
            messages,
            system_prompt=_system_prompt_for_context(context),
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
            system_prompt=_system_prompt_for_context(context),
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
            self._endpoint(),
            headers=self._headers(),
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
            "completion_tokens": (
                payload.get("usage", {}).get("output_tokens")
                if isinstance(payload.get("usage"), dict)
                else None
            ),
            "surface": "messages",
            "http_status": getattr(resp, "status_code", 200),
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


def _decision_attempt(text: Any, error: Exception, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "error": f"{type(error).__name__}: {error}",
        "preview": _clip_text(text, 800),
        "response": dict(metadata),
    }
