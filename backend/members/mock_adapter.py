from __future__ import annotations

import json

from backend.core.config import LLMConfig
from backend.members.adapters import BaseAdapter, MemberAction


class MockAdapter(BaseAdapter):
    def __init__(self, config: LLMConfig, name: str = "agent", script: list[dict] | None = None):
        super().__init__(config, name=name)
        self._script = list(script) if script else None

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

    def _default(self, context: dict) -> MemberAction:
        step = context.get("step", 1)
        intent_desc = context.get("assigned_intent", {}).get("description", "explore")
        category = context.get("category", "misc")
        is_initial = context.get("is_initial", False)

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
