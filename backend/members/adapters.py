from __future__ import annotations

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
    def from_obj(cls, obj: dict[str, Any]) -> "MemberAction":
        kind = obj.get("action") or obj.get("kind")
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
            if "args" not in args:
                args["args"] = {}
        return cls(kind=kind, args=args, thought=obj.get("thought", ""))


class BaseAdapter:
    def __init__(self, config: LLMConfig, name: str = "agent"):
        self.config = config
        self.name = name

    def health(self) -> dict:
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
    "You are working inside a short exploration task: each run has only some actions, "
    "so produce a clear result quickly. End with conclude for a confirmed fact, flag for a real flag, "
    "intent for a concrete next direction, or report when blocked; do not silently spin. "
    "At the start of each CTF round, check likely flag sources in this order: first try reading /flag, "
    "then inspect challenge environment variables(FLAG or check ENV), then continue with challenge-specific methods. "
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
        import requests

        resp = requests.post(
            self._endpoint(),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.config.model or "gpt-4o",
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                "temperature": 0.4,
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return MemberAction.from_obj(_extract_json(text))


class ClaudeAdapter(BaseAdapter):

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
        import requests

        resp = requests.post(
            f"{self.config.base_url.rstrip('/')}/v1/messages",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": self.config.model or "claude-opus-4-8",
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        return MemberAction.from_obj(_extract_json(text))


class PiAdapter(OpenAICompatibleAdapter):
    """Pi uses an OpenAI-compatible surface in this build."""

    def health(self) -> dict:
        result = super().health()
        result["format"] = "pi"
        return result


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # find first {...}
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON action found in model output")
