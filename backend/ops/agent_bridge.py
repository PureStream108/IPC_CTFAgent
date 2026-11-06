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
from backend.platform.presets import preset_tool_names

PRESET_WORKFLOW_TOOLS = preset_tool_names()
PROPOSE_WORKFLOW_TOOL = {
    "name": "propose_platform_workflow",
    "description": (
        "Propose a declarative platform workflow. The workflow is saved as a "
        "draft and needs no confirmation phrase: the operator reviews the stored "
        "URL, method and JSON template and starts the run themselves. Never put "
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
        if name in PRESET_WORKFLOW_TOOLS and result.get("id"):
            workflow_id = str(result["id"])
            if not any(item.get("id") == workflow_id for item in self.proposals):
                self.proposals.append(result)
        self.tool_events.append(
            {
                "name": name,
                "project_id": arguments.get("project_id"),
                "ok": bool(result.get("ok", True)),
            }
        )
        if name == "question" and result.get("state") == "pending":
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
            "note": "Saved as a draft; preflight it, then let the operator press Start.",
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


COMPACTION_CHUNK_CHARS = 48_000
# Stay under the runtime's own summary budget (8 KiB by default) so a merged
# summary is never replaced by a truncated text blob.
COMPACTION_SUMMARY_BYTES = 6_000
COMPACTION_LIST_LIMITS = {
    "verified_facts": 24,
    "failed_paths": 16,
    "next_hypotheses": 12,
    "artifacts": 12,
    "open_questions": 8,
    "key_offsets": 8,
}


def _compactable_text(message: dict[str, Any], limit: int = 2_000) -> str:
    """Render one transcript message small enough to summarize in a chunk."""

    content = message.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False) if content is not None else ""
    if len(content) > limit:
        content = content[:limit] + f"\n[...{len(content) - limit} chars truncated]"
    calls = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") or {}
            calls.append(str(function.get("name") or "tool"))
    head = message.get("role") or "message"
    if calls:
        head += " [calls: " + ", ".join(calls) + "]"
    return f"{head}: {content}"


def _chunk_transcript(dropped: list[dict[str, Any]], budget: int) -> list[str]:
    """Split a dropped transcript into provider-friendly summarization chunks."""

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for message in dropped:
        if not isinstance(message, dict):
            continue
        piece = _compactable_text(message)
        if current and size + len(piece) > budget:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _merge_summaries(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Fold per-chunk summaries into one, dropping exact duplicate claims."""

    for key, values in right.items():
        bucket = left.setdefault(key, [])
        seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in bucket}
        for item in values:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker not in seen:
                seen.add(marker)
                bucket.append(item)
    return left


def _clip_value(value: Any, limit: int = 320) -> Any:
    """Shrink one summary value so a merged summary stays readable."""

    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 3] + "..."
    if isinstance(value, dict):
        return {key: _clip_value(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip_value(item, limit) for item in value[:8]]
    return value


def _tighten_summary(summary: dict[str, Any], budget: int) -> dict[str, Any]:
    """Cap list lengths and entry sizes until the summary fits the budget."""

    tightened: dict[str, Any] = {}
    for key, values in summary.items():
        if isinstance(values, list):
            tightened[key] = [
                _clip_value(item) for item in values[: COMPACTION_LIST_LIMITS.get(key, 8)]
            ]
        else:
            tightened[key] = _clip_value(values)
    while len(json.dumps(tightened, ensure_ascii=False).encode("utf-8")) > budget:
        droppable = [
            key
            for key, values in tightened.items()
            if isinstance(values, list) and len(values) > 1
        ]
        if not droppable:
            break
        # Trim the longest list first: it is the one that grew across chunks.
        target = max(droppable, key=lambda key: len(tightened[key]))
        tightened[target].pop()
    return tightened


def build_ops_compressor(config, *, instructions: str = ""):
    """Summarize dropped Ops history with the Ops conversation's own model.

    The optional instructions argument is the operator's /compact note ("keep
    the workflow decisions, drop the curl attempts"). It is appended to the
    compaction prompt so a manual compaction can steer what survives.

    A long conversation is summarized in chunks and the per-chunk summaries are
    merged: a single request carrying a 180k-token transcript either times out
    or comes back unusable, which silently degrades to the deterministic
    projection and keeps nothing worth keeping.
    """
    from backend.agent.compaction import (
        _parse_json,
        is_empty,
        normalize_summary,
        summarize,
    )
    from backend.members.adapters import make_adapter

    extra = (instructions or "").strip()[:2000]

    def compress(messages: list[dict[str, Any]]):
        metadata: dict[str, Any] = {}

        def request(prompt: str, dropped: list[dict[str, Any]]) -> str:
            adapter = make_adapter(config, name="ops-compaction")
            system_prompt = (
                prompt
                + "\n\nKeep it dense: at most 12 entries per list and at most "
                "200 characters per entry."
            )
            if extra:
                system_prompt += (
                    "\n\nOperator instructions for this compaction (follow them "
                    "when choosing what to keep):\n" + extra
                )
            merged: dict[str, Any] = {}
            failures = 0
            chunks = _chunk_transcript(dropped, COMPACTION_CHUNK_CHARS)
            for chunk in chunks:
                part: dict[str, Any] | None = None
                for _attempt in range(2):
                    try:
                        raw = adapter.chat(
                            [{"role": "user", "content": chunk}],
                            system_prompt=system_prompt,
                            temperature=0.0,
                        )
                    except Exception:  # noqa: BLE001, S112 - one bad chunk must not abort compaction
                        continue
                    candidate = normalize_summary(_parse_json(raw))
                    if not is_empty(candidate):
                        part = candidate
                        break
                if part is None:
                    failures += 1
                    continue
                merged = _merge_summaries(merged, part)
            metadata["chunks"] = len(chunks)
            if failures:
                metadata["degraded_reason"] = (
                    f"{failures} of {len(chunks)} compaction chunks returned no usable summary"
                )
            if not merged:
                raise ValueError("the summarizer produced no usable summary")
            return json.dumps(
                _tighten_summary(merged, COMPACTION_SUMMARY_BYTES), ensure_ascii=False
            )

        result = summarize(messages, request=request)
        compress.last_metadata = metadata
        return result

    compress.last_metadata = {}
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
    emit_reasoning=None,
    log=None,
    runtime_config=None,
    compaction_instructions: str = "",
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
        emit_reasoning=emit_reasoning,
        compressor=build_ops_compressor(
            config, instructions=compaction_instructions
        ),
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
