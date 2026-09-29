"""Ops chat on the unified agent runtime, using native provider tool calls.

Before this module, Ops asked the model to emit ``{"reply":...,"tool_call":...}``
as JSON inside prose, then re-encoded each round as alternating user/assistant
text. That is what lost the tool transcript: the durable conversation kept only
the final reply, so the next turn could not see what the model had already run.

Here tool calls go over the provider's own tool-call channel and every
provider message, call and result becomes an ``agent_events`` row, so the next
turn's context is rebuilt from the real transcript.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from backend.agent.runtime import AgentRuntime, AgentSessionWriter
from backend.agent.session import (
    AgentSessionStore,
    SessionConflict,
    ops_task_key,
    redact_model_snapshot,
)
from backend.competition.conversation import ConversationAdapter

# The workflow proposal used to be a field the model wrote into its final JSON
# reply, which meant parsing prose to find it. It is an action, so it is a tool.
PROPOSE_WORKFLOW_TOOL = {
    "name": "propose_platform_workflow",
    "description": (
        "Propose a declarative platform workflow for operator confirmation. "
        "The workflow is saved as a draft and is never executed until the "
        "operator confirms its exact URL, method and JSON template. Never put "
        "literal credentials in any field; use a {{secret.NAME}} alias."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "description": "A PlatformWorkflowSpec object.",
                "additionalProperties": True,
            }
        },
        "required": ["spec"],
        "additionalProperties": False,
    },
}


class OpsSessionWriter(AgentSessionWriter):
    """Agent-session writer that also mirrors events to the Ops live log."""

    def __init__(self, store, session_id, owner, epoch, *, log=None) -> None:
        super().__init__(store, session_id, owner, epoch)
        self._log = log

    def append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        row = super().append(event_key, kind, payload)
        if self._log is not None:
            self._log(kind, payload)
        return row


class OpsToolBridge:
    """Adapt :class:`OpsToolExecutor` to the runtime's executor protocol."""

    def __init__(self, service, session_id: str, *, on_event=None) -> None:
        self.service = service
        self.session_id = session_id
        self.on_event = on_event
        self.tool_events: list[dict[str, Any]] = []
        self.proposals: list[dict[str, Any]] = []
        self.proposal_errors: list[str] = []
        self.pending_question = False

    def execute(
        self, name: str, arguments: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        del idempotency_key
        if name == PROPOSE_WORKFLOW_TOOL["name"]:
            return self._propose(arguments)
        result = self.service._run_ops_tool(name, arguments, self.session_id)
        self.tool_events.append(
            {
                "name": name,
                "project_id": arguments.get("project_id"),
                "ok": bool(result.get("ok", True)),
            }
        )
        if name == "question" and result.get("state") == "pending":
            # The operator has to answer before the dependent operation can
            # continue, so the loop stops here instead of guessing.
            self.pending_question = True
        if self.on_event is not None:
            self.on_event(name, arguments, result)
        return result

    def _propose(self, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = arguments.get("spec")
        if not isinstance(spec, dict):
            return {"ok": False, "error": "spec must be an object"}
        try:
            view = self.service._save_workflow_proposal(spec, self.session_id)
        except (TypeError, ValueError) as exc:
            message = f"The proposed workflow was not saved: {exc}"
            self.proposal_errors.append(message)
            return {"ok": False, "error": message}
        self.proposals.append(view)
        return {
            "ok": True,
            "workflow_id": view.get("id"),
            "status": view.get("status"),
            "note": "Saved as a draft; the operator must confirm it before execution.",
        }


def ensure_ops_agent_session(
    agent_store: AgentSessionStore,
    ops_store,
    session_id: str,
    config,
    *,
    bootstrap_history: list[dict[str, Any]] | None = None,
) -> dict:
    """Return the agent session for an Ops conversation, creating it once.

    An Ops conversation that predates this runtime has durable messages but no
    tool transcript, because the old protocol never persisted one. Seeding it
    as a single bootstrap ``user_message`` is the only faithful reconstruction
    available: it preserves what was actually said without inventing tool calls
    that were never recorded.
    """
    existing = ops_store.agent_session_id(session_id)
    if existing:
        try:
            return agent_store.session(existing)
        except KeyError:
            # The row was removed (for example by a database reset) while the
            # conversation kept the dangling pointer.
            ops_store.set_agent_session_id(session_id, None)

    snapshot = redact_model_snapshot(
        {
            "api_format": config.api_format,
            "api_surface": config.api_surface,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "base_url": config.base_url,
        }
    )
    task_key = ops_task_key(session_id)
    try:
        session = agent_store.create_session(
            "ops", task_key, model_snapshot=snapshot
        )
    except SessionConflict:
        session = agent_store.active_session_for_task(task_key)
        if session is None:
            raise
    ops_store.set_agent_session_id(session_id, session["id"])

    if bootstrap_history:
        agent_store.claim_writer(session["id"], "ops-run", "bootstrap", takeover=True)
        rendered = "\n\n".join(
            f"{item['role']}: {item['content']}" for item in bootstrap_history
        )
        agent_store.append_event(
            session["id"],
            "session:bootstrap",
            "user_message",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Durable history of this conversation from before "
                            "the unified runtime. Tool calls made then were not "
                            "recorded and are not available:\n" + rendered
                        ),
                    }
                ]
            },
            owner="bootstrap",
            epoch=1,
        )
    return session


def build_ops_compressor(config):
    """Summarize dropped Ops history with the Ops conversation's own model."""
    from backend.agent.compaction import summarize
    from backend.members.adapters import make_adapter

    def request(prompt: str, dropped: list[dict[str, Any]]) -> str:
        adapter = make_adapter(config, name="ops-compaction")
        rendered = json.dumps(dropped, ensure_ascii=False)[:400_000]
        return adapter.chat(
            [{"role": "user", "content": rendered}],
            system_prompt=prompt,
            temperature=0.0,
        )

    def compress(messages: list[dict[str, Any]]):
        return summarize(messages, request=request)

    return compress


def build_ops_runtime(
    *,
    config,
    agent_store: AgentSessionStore,
    session: dict,
    owner: str,
    epoch: int,
    system: str,
    tools: list[dict[str, Any]],
    executor,
    cancel: threading.Event | None = None,
    emit=None,
    log=None,
    runtime_config=None,
) -> AgentRuntime:
    limits = runtime_config
    adapter = ConversationAdapter(
        config,
        max_output_tokens=getattr(limits, "max_output_tokens", 32768),
        read_timeout=getattr(limits, "provider_read_timeout", 300),
        provider_session_id=session.get("provider_session_id"),
    )
    writer = OpsSessionWriter(
        agent_store, session["id"], owner, epoch, log=log
    )
    return AgentRuntime(
        writer,
        adapter,
        executor,
        system=system,
        tools=tools,
        cancel=cancel,
        emit=emit,
        compressor=build_ops_compressor(config),
        max_turns=getattr(limits, "max_turns", 0),
        context_token_limit=getattr(limits, "context_token_limit", 0),
        compaction_trigger_ratio=getattr(limits, "compaction_trigger_ratio", 0.75),
        calibration=float(
            (session.get("token_budget") or {}).get("calibration_ratio") or 1.0
        ),
    )


def redact(value: Any, secret_values: dict[str, str]) -> str:
    """Render a tool payload for the live log with secret values replaced."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for name, secret in secret_values.items():
        if secret:
            text = text.replace(secret, f"{{{{secret.{name}}}}}")
    return text
