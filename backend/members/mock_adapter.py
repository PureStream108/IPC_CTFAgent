from __future__ import annotations

import json

from backend.core.config import LLMConfig
from backend.members.adapters import BaseAdapter, MemberAction
from backend.members.member_tools import tool_call_for_action


class MockAdapter(BaseAdapter):
    def __init__(self, config: LLMConfig, name: str = "agent", script: list[dict] | None = None):
        super().__init__(config, name=name)
        self._script = list(script) if script else None
        self._reported = False
        self._stream_script = list(script) if script else None
        self._stream_calls = 0

    def health(self) -> dict:
        return {"ok": True, "status": 200, "format": "mock"}

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system_prompt: str = "",
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        latest = messages[-1]["content"] if messages else ""
        return json.dumps(
            {
                "reply": f"Mock Ops Agent received: {latest}",
                "workflow": None,
            },
            ensure_ascii=False,
        )

    def decide(self, context: dict) -> MemberAction:
        if self._script is not None:
            if self._script:
                return MemberAction.from_obj(self._script.pop(0))
            return MemberAction(kind="done", args={"reason": "script exhausted"})
        return self._default(context)


    provider_session_id = "mock-session"

    def tool_result(self, call, output) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(output, ensure_ascii=False),
        }

    def turn(self, messages, system, tools, emit, cancel, emit_reasoning=None):
        """Produce one scripted turn as a native tool call, or finish idle.

        Each scripted action maps to exactly one tool call via
        ``tool_call_for_action`` (the inverse of ``action_payload``), so the
        runtime's executor round-trips it back to the identical
        ``MemberAction`` the legacy loop would have dispatched. With no script,
        or once it is exhausted, the mock returns a plain assistant message and
        no tools, which the runtime treats as an idle finish.
        """
        from backend.competition.conversation import ToolCall, Turn

        if self._stream_script:
            action = self._stream_script.pop(0)
            name, arguments = tool_call_for_action(action)
            thought = arguments.get("thought", "")
            call = ToolCall(
                id=f"mock-{self._stream_calls}", name=name, arguments=arguments
            )
            self._stream_calls += 1
            if thought:
                emit(thought)
            assistant = {
                "role": "assistant",
                "content": thought or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, ensure_ascii=False),
                        },
                    }
                ],
            }
            return Turn([assistant], tools=[call], text=thought or "")
        latest = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                latest = content if isinstance(content, str) else json.dumps(
                    content, ensure_ascii=False
                )
                break
        text = f"Mock member received: {latest}"
        emit(text)
        return Turn([{"role": "assistant", "content": text}], text=text)

    def _default(self, context: dict) -> MemberAction:
        step = context.get("step", 1)
        intent_desc = context.get("assigned_intent", {}).get("description", "explore")
        category = context.get("category", "misc")
        is_initial = context.get("is_initial", False)
        evaluate_now = context.get("evaluate_now", False)

        if evaluate_now and not self._reported:
            self._reported = True
            difficulty = "high" if is_initial else "medium"
            return MemberAction(
                kind="report",
                thought=f"step {step}: evaluating difficulty of '{intent_desc}'",
                args={
                    "progress": f"explored '{intent_desc}', {step} steps in",
                    "difficulty": difficulty,
                    "steps": [f"recon on {category} target", f"analysed '{intent_desc}'"],
                    "directions": [f"investigate alternate {category} vector", "check component CVEs"],
                    "knowledge": [category, "recon"],
                },
            )

        if step == 1:
            return MemberAction(kind="bash", thought="initial recon",
                                args={"command": f"echo recon on {category} challenge"})
        if step == 2:
            return MemberAction(kind="memory", thought="recall past experience",
                                args={"query": f"{category} {intent_desc}"})
        if is_initial and step >= 3:
            return MemberAction(
                kind="flag",
                thought="assembled the exploit, captured the flag",
                args={
                    "flag": context.get("expected_flag") or "flag{mock_solved}",
                    "description": f"Captured flag via '{intent_desc}'.",
                },
            )
        if step == 3:
            return MemberAction(
                kind="conclude",
                thought="confirmed a concrete result for the assigned intent",
                args={"description": f"Confirmed result while exploring '{intent_desc}' ({category})."},
            )
        return MemberAction(kind="done", thought="nothing further to explore",
                            args={"reason": "exploration exhausted"})
