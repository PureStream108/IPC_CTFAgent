from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.difficulty import (
    DIFFICULTY_RANK,
    detect_attack_surfaces,
    detect_exploit_classes,
    max_difficulty,
    normalize_difficulty,
)
from backend.core.logging_util import IPCLogger
from backend.core.ipc import FlagConflictError, submit_flag_candidate
from backend.core.postprocess_store import enqueue_postprocess
from backend.mcp.mcp_client import MCPRegistry, MCPRegistrySession, MCPRegistryTarget
from backend.members.adapters import (
    ACTION_KINDS,
    BaseAdapter,
    DecisionOutputError,
    MemberAction,
    ProviderError,
)
from backend.competition.runtime import SessionRunner
from backend.competition.store import CompetitionConflict
from backend.competition.conversation import ConversationAdapter, ToolCall
from backend.competition.transport import ReconSubscriber
from backend.memory.memory_search import search as mem_search
from backend.memory.memory_store import MemoryStore
from backend.sandbox.sandbox import Sandbox
from backend.tools.tool_mcp import build_category_tools_mcp, build_tool_search_mcp
from backend.tools.tool_inventory import member_tool_inventory, member_tool_inventory_path
from backend.tools.tool_registry import LANGUAGES, PUBLIC_MCPS, ToolRegistry

_LOCAL_WEBUI_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d{2,5})\b")
_PORT_FLAG_RE = re.compile(r"(?:^|\s)(?:--port|-p)\s+(\d{2,5})(?:\s|$)")
_WEBUI_HINT_RE = re.compile(r"\b(webui|gradio|streamlit|jupyter|flask|uvicorn)\b", re.IGNORECASE)
_COMMAND_NOT_FOUND_RE = re.compile(
    r"(?m)(?:^|: )([A-Za-z0-9_.+-]+): (?:command not found|not found)\b"
)
_WINDOWS_COMMAND_NOT_FOUND_RE = re.compile(r"'([^']+)' is not recognized", re.IGNORECASE)
_CLI_PROBE_TOOLS = (
    "rg", "grep", "find", "git", "curl", "wget", "jq", "python3", "python",
    "file", "strings", "xxd", "unzip", "zip", "openssl", "nmap", "gdb",
    "sqlmap", "sage", "node", "npm", "php", "ruby",
)


@dataclass
class MemberDeps:
    db: Any
    logger: IPCLogger
    sandbox: Sandbox
    mcps: MCPRegistry
    registry: ToolRegistry
    memory: MemoryStore
    container_mcps: dict[str, MCPRegistryTarget] | None = None
    eval_interval: int = 7
    max_steps: int = 60
    max_actions_per_task: int = 4
    continuous: bool = False
    on_report: Callable[[str, Any], None] | None = None   # (project_id, Report)
    on_flag: Callable[[str], None] | None = None           # (project_id)
    expected_flag: str | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    # Set only for competition assignments.  The legacy graph dispatcher can
    # then share the durable competition session/event protocol without
    # changing standalone project behavior.
    competition_store: Any | None = None
    competition_assignment: dict[str, Any] | None = None
    # Real competition assignments may opt into the provider-native streamed
    # loop.  Standalone projects and mock adapters continue using the legacy
    # graph dispatcher unless this explicit bridge is enabled.
    competition_native: bool = False
    competition_conversation_factory: Callable[[Any], Any] | None = None
    competition_recon_endpoint: str | None = None
    competition_recon_replay: Callable[..., list[dict[str, Any]]] | None = None
    competition_recon_advance: Callable[[str, int], int] | None = None


@dataclass
class SolveResult:
    status: str          # concluded | flag | done | stalled | failed | stopped
    steps: int
    fact_id: str | None = None
    flag: str | None = None
    error: str | None = None
    retryable: bool | None = None
    error_kind: str | None = None


@dataclass
class DispatchResult:
    result: SolveResult | None = None
    graph_action: str | None = None
    invalid_action: bool = False
    invalid_knowledge: list[str] | None = None
    # Latest observation produced by the action. The provider-native tool
    # loop returns it as the tool result so the model actually sees command
    # output instead of only a status envelope.
    observation: str | None = None


class _IntentLeaseLost(RuntimeError):
    """Abort the surrounding transaction when a member loses its fence."""


_NATIVE_HISTORY_TARGET_BYTES = 512_000
_NATIVE_HISTORY_MIN_BLOCKS = 6
_NATIVE_TOOL_CLIP_BYTES = 8_000
_NATIVE_TOOL_CLIP_KEEP = 3_000


def _history_bytes(messages: list[dict[str, Any]]) -> int:
    return len(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()
    )


def _history_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group an assistant tool-call message with the tool results it owns."""

    blocks: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        block = [message]
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("tool_calls")
        ):
            call_ids = {
                str(call.get("id"))
                for call in message.get("tool_calls") or []
                if isinstance(call, dict)
            }
            index += 1
            while index < len(messages):
                candidate = messages[index]
                if (
                    isinstance(candidate, dict)
                    and candidate.get("role") == "tool"
                    and str(candidate.get("tool_call_id")) in call_ids
                ):
                    block.append(candidate)
                    index += 1
                    continue
                break
            blocks.append(block)
            continue
        index += 1
        blocks.append(block)
    return blocks


def _clip_message_content(message: dict[str, Any], limit: int) -> None:
    content = message.get("content")
    if isinstance(content, str) and len(content) > limit:
        message["content"] = content[:limit] + "\n[older output elided]"


def compact_native_history(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Deterministically shrink a provider-native transcript to fit context.

    Long tool outputs are elided first; when the transcript is still over the
    target, the oldest complete blocks (an assistant tool-call message plus
    its tool results) are dropped while the first user message and the most
    recent exchanges stay intact so the next turn can continue.
    """

    kept = [dict(message) for message in messages]
    for message in kept:
        _clip_message_content(message, _NATIVE_TOOL_CLIP_BYTES)
    if _history_bytes(kept) <= _NATIVE_HISTORY_TARGET_BYTES:
        return "elided older tool outputs from the native history", kept

    blocks = _history_blocks(kept)
    head = blocks[:1]
    tail = blocks[1:]
    summary = "dropped the oldest native conversation blocks to fit the provider context"
    while (
        len(tail) > _NATIVE_HISTORY_MIN_BLOCKS
        and _history_bytes(head + tail) > _NATIVE_HISTORY_TARGET_BYTES
    ):
        tail.pop(0)
    compacted = [message for block in head + tail for message in block]
    if _history_bytes(compacted) > _NATIVE_HISTORY_TARGET_BYTES:
        for message in compacted:
            _clip_message_content(message, _NATIVE_TOOL_CLIP_KEEP)
    return summary, compacted


class BaseMember:
    role_blurb = "a versatile CTF solver"

    def __init__(self, name: str, adapter: BaseAdapter, deps: MemberDeps):
        self.name = name
        self.adapter = adapter
        self.deps = deps
        self._stop = threading.Event()
        self.observations: list[str] = []
        self._recent_action_sigs: deque[str] = deque(maxlen=12)
        self._pending_bumps: list[str] = []
        self._tool_availability: dict[str, bool] | None = None
        self._missing_tool_counts: dict[str, int] = {}
        self._state_lock = threading.Lock()
        self._progress_path: str | None = None
        self._progress_project_id: str | None = None
        self._progress_save_error_reported = False
        self._session_runner: SessionRunner | None = None
        self._recon_subscriber: ReconSubscriber | None = None
        self._session_recording_failed = False

    def _competition_session(self) -> SessionRunner | None:
        if self._session_runner is not None:
            return self._session_runner
        store = self.deps.competition_store
        assignment = self.deps.competition_assignment
        if store is None or assignment is None:
            return None
        self._session_runner = SessionRunner(
            store,
            None,
            None,
            assignment=assignment,
            system=f"Persistent competition session for Member {self.name}",
            tools=[],
        )
        return self._session_runner

    def _record_competition_event(self, callback) -> bool:
        runner = self._competition_session()
        if runner is None or self._session_recording_failed:
            return True
        try:
            callback(runner)
            return True
        except Exception as exc:
            self._session_recording_failed = True
            self._stop.set()
            self.deps.logger.project(
                "competition_session_persistence_failed",
                self._progress_project_id or "unknown",
                member=self.name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def stop(self) -> None:
        self._stop.set()
        if self._recon_subscriber is not None:
            self._recon_subscriber.close()
            self._recon_subscriber = None

    def _competition_recon(self) -> ReconSubscriber | None:
        if self._recon_subscriber is not None:
            return self._recon_subscriber
        d = self.deps
        if (
            getattr(d, "competition_assignment", None) is None
            or getattr(d, "competition_recon_replay", None) is None
            or getattr(d, "competition_recon_advance", None) is None
        ):
            return None
        assignment = d.competition_assignment
        self._recon_subscriber = ReconSubscriber(
            getattr(d, "competition_recon_endpoint", None),
            str(assignment["challenge_id"]),
            session_id=str(assignment["session_id"]),
            replay=d.competition_recon_replay,
            advance=d.competition_recon_advance,
        )
        return self._recon_subscriber

    def _drain_competition_recon(self, runner: SessionRunner | None = None) -> list[dict[str, Any]]:
        """Persist received recon into the session before acknowledging it."""
        subscriber = self._competition_recon()
        if subscriber is None:
            return []
        messages: list[dict[str, Any]] = []
        while True:
            record = subscriber.receive(timeout_ms=0)
            if record is None:
                break
            envelope = record.get("envelope", record)
            payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
            sequence = int(record["sequence"])
            message = {
                "role": "user",
                "content": (
                    "Durable reconnaissance from a teammate. Use it as evidence and "
                    "avoid repeating the same action:\n"
                    + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                ),
            }
            if runner is not None:
                runner.record_provider_message(
                    message,
                    event_key=f"recon:{sequence}",
                )
            messages.append(message)
            subscriber.ack(sequence)
        return messages

    def solve(self, project_id: str, intent_id: str, category: str, is_initial: bool = False) -> SolveResult:
        """Synchronous compatibility entry point for non-async callers."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.solve_async(project_id, intent_id, category, is_initial))
        raise RuntimeError("solve() cannot run inside an event loop; await solve_async() instead")

    async def solve_async(
        self,
        project_id: str,
        intent_id: str,
        category: str,
        is_initial: bool = False,
    ) -> SolveResult:
        available_mcps = self._available_mcp_names()
        category_tools = build_category_tools_mcp(
            self.deps.registry,
            category,
            available_mcps=available_mcps,
        )
        tool_search = build_tool_search_mcp(
            self.deps.registry,
            available_mcps=available_mcps,
        )
        extra_mcps: dict[str, MCPRegistryTarget] = {
            "tools": category_tools,
            "tool_search": tool_search,
        }
        extra_mcps.update(self.deps.container_mcps or {})
        async with self.deps.mcps.session(extra_mcps) as mcp_session:
            return await self._solve_with_mcp(
                project_id,
                intent_id,
                category,
                is_initial,
                mcp_session,
            )

    async def _solve_with_mcp(
        self,
        project_id: str,
        intent_id: str,
        category: str,
        is_initial: bool,
        mcp_session: MCPRegistrySession,
    ) -> SolveResult:
        d = self.deps
        self._restore_progress(project_id, intent_id)
        d.logger.project(
            "member_start",
            project_id,
            member=self.name,
            intent=intent_id,
            initial=is_initial,
            restored_observations=len(self.observations),
        )
        if self._native_competition_enabled():
            return await self._solve_with_native_session(
                project_id,
                intent_id,
                category,
                is_initial,
                mcp_session,
            )
        session_runner = self._competition_session()
        if session_runner is not None:
            try:
                self._drain_competition_recon(session_runner)
                existing = session_runner.store.events(session_runner.session_id, limit=1)
                if not existing:
                    session_runner.record_user_message(
                        f"Begin the persistent {category} challenge session for intent {intent_id}."
                    )
            except Exception as exc:
                self._session_recording_failed = True
                d.logger.project(
                    "competition_session_restore_failed",
                    project_id,
                    member=self.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return SolveResult(
                    status="stalled", steps=0,
                    error="competition session could not be restored",
                )
        if not self._claim(project_id, intent_id):
            d.logger.project(
                "intent_claim_lost",
                project_id,
                member=self.name,
                intent=intent_id,
            )
            return SolveResult(status="stalled", steps=0, error="intent lease unavailable")
        self._seed_tool_inventory(project_id)
        self._prime_tool_context(project_id, intent_id, category)
        step = 0
        task_budget = max(1, min(d.max_steps, d.max_actions_per_task))
        graph_actions: list[str] = []
        branch_intents = 0
        invalid_actions = 0
        while (d.continuous or step < task_budget) and not self._stop.is_set():
            step += 1
            if session_runner is not None:
                self._drain_competition_recon(session_runner)
            evaluate_now = step % d.eval_interval == 0
            context = self._build_context(project_id, intent_id, category, step, is_initial, evaluate_now)
            try:
                action = self.adapter.decide(context)
            except DecisionOutputError as exc:
                d.logger.llm(
                    "decision_parse_error",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    step=step,
                    attempts=exc.attempts,
                )
                d.logger.project(
                    "member_error",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    error=str(exc),
                    retryable=True,
                )
                self._release(project_id, intent_id)
                return SolveResult(
                    status="failed",
                    steps=step,
                    error=str(exc),
                    retryable=True,
                    error_kind="model_output",
                )
            except ProviderError as exc:
                error = f"{type(exc).__name__}: {exc}"
                d.logger.project(
                    "member_error",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    error=error,
                    retryable=exc.retryable,
                    provider=exc.provider,
                    status_code=exc.status_code,
                )
                self._release(project_id, intent_id)
                return SolveResult(
                    status="failed",
                    steps=step,
                    error=error,
                    retryable=exc.retryable,
                    error_kind="provider",
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                d.logger.project(
                    "member_error",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    error=error,
                    retryable=True,
                )
                self._release(project_id, intent_id)
                return SolveResult(
                    status="failed",
                    steps=step,
                    error=error,
                    retryable=True,
                    error_kind="member_runtime",
                )
            # ``decide`` may be a blocking network call.  An operator can stop
            # the project while it is in flight; honour that interrupt before
            # dispatching the returned shell/MCP action, otherwise the action
            # could recreate a task container that ``stop_project`` removed.
            if self._stop.is_set():
                self._release(project_id, intent_id)
                d.logger.project(
                    "member_stopped",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    steps=step,
                )
                return SolveResult(status="stopped", steps=step)
            d.logger.llm("decide", project_id, member=self.name, step=step,
                         thought=action.thought, action=action.kind)
            # A model call may outlive the intent lease.  Fence the result
            # before dispatching it so a stale worker cannot execute shell/MCP
            # actions or write graph state after another worker takes over.
            if not self._heartbeat(project_id, intent_id):
                self._release(project_id, intent_id)
                d.logger.project(
                    "intent_lease_lost",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    steps=step,
                )
                return SolveResult(status="stalled", steps=step, error="intent lease lost")
            loop_status = self._record_action_signature(action)
            if loop_status == "warn":
                self._observe(
                    "[stuckness] You have repeated the same action signature several times. "
                    "Stop replaying it and switch to a distinct exploit class, tool, or evidence source."
                )
                d.logger.project(
                    "member_loop_warning",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    steps=step,
                )
            if loop_status == "break":
                self._submit_stall_report(
                    project_id,
                    intent_id,
                    category,
                    step,
                    difficulty_hint="medium",
                    extra_knowledge=["action_signature_repeat"],
                )
                self._release(project_id, intent_id)
                d.logger.project("member_loop_detected", project_id, member=self.name, intent=intent_id, steps=step)
                return SolveResult(status="stalled", steps=step)

            # Persist the model action before dispatching its side effect.  A
            # restarted worker can then distinguish a completed tool call from
            # one that was claimed but never returned, instead of blindly
            # issuing the same shell/MCP operation twice.
            session_call: ToolCall | None = None
            if session_runner is not None:
                signature = hashlib.sha256(
                    json.dumps(
                        {"kind": action.kind, "args": action.args},
                        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:24]
                call_id = f"legacy:{intent_id}:{signature}"
                session_call = ToolCall(call_id, action.kind, action.args)
                prior = session_runner.call_state(call_id)
                if prior == "done":
                    self._observe("[replayed competition action result restored from durable session]")
                    continue
                if prior == "pending":
                    # The previous process may have executed the side effect
                    # immediately before crashing.  Surface uncertainty to the
                    # model and require an explicit, new action/evidence path.
                    unknown = {"status": "unknown", "reason": "worker restarted during tool execution"}
                    session_runner.record_tool_result(
                        session_call,
                        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(unknown)},
                    )
                    self._observe("[previous competition tool call is unknown; do not replay it]")
                    continue
                if not self._record_competition_event(
                    lambda runner: runner.record_provider_message(
                        {"role": "assistant", "content": json.dumps(
                            {"action": action.kind, "arguments": action.args},
                            ensure_ascii=False,
                        )},
                        event_key=f"provider:{call_id}",
                        text=action.thought,
                    )
                ):
                    self._release(project_id, intent_id)
                    return SolveResult(status="stalled", steps=step, error="session lease lost")
                if not self._record_competition_event(
                    lambda runner: runner.record_tool_call(session_call)
                ):
                    self._release(project_id, intent_id)
                    return SolveResult(status="stalled", steps=step, error="session lease lost")

            try:
                dispatched = await self._dispatch(
                        project_id,
                        intent_id,
                        category,
                        action,
                        step,
                        mcp_session,
                        allow_intent=branch_intents < 1,
                    )
            except FlagConflictError as exc:
                self._release(project_id, intent_id)
                d.logger.project(
                    "member_flag_conflict",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    error=str(exc),
                )
                return SolveResult(
                    status="stalled",
                    steps=step,
                    error=str(exc),
                    retryable=False,
                    error_kind="flag_conflict",
                )
            except ValueError as exc:
                # Dispatch-time validation failures are terminal for this
                # attempt.  Retrying the same action only replays the invalid
                # graph transition (for example, a missing completion edge or
                # an already-solved project) and can create a retry storm.
                self._release(project_id, intent_id)
                d.logger.project(
                    "member_validation_error",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    error=str(exc),
                    retryable=False,
                    error_kind="terminal_validation",
                )
                return SolveResult(
                    status="stalled",
                    steps=step,
                    error=str(exc),
                    retryable=False,
                    error_kind="terminal_validation",
                )
            if session_runner is not None and session_call is not None:
                result_payload = {
                    "status": "ok",
                    "graph_action": dispatched.graph_action,
                    "invalid_action": dispatched.invalid_action,
                    "terminal": dispatched.result.status if dispatched.result else None,
                }
                if not self._record_competition_event(
                    lambda runner: runner.record_tool_result(
                        session_call,
                        {
                            "role": "tool",
                            "tool_call_id": session_call.id,
                            "content": json.dumps(result_payload, ensure_ascii=False),
                        },
                    )
                ):
                    self._release(project_id, intent_id)
                    return SolveResult(status="stalled", steps=step, error="session lease lost")
            if dispatched.graph_action is not None:
                graph_actions.append(dispatched.graph_action)
                if dispatched.graph_action == "intent":
                    branch_intents += 1
            if dispatched.invalid_action:
                invalid_actions += 1
                if invalid_actions >= 2:
                    invalid_knowledge = dispatched.invalid_knowledge or []
                    self._submit_stall_report(
                        project_id,
                        intent_id,
                        category,
                        step,
                        difficulty_hint="medium",
                        extra_knowledge=["invalid_action_contract", *invalid_knowledge],
                    )
                    self._release(project_id, intent_id)
                    d.logger.project(
                        "member_invalid_action_limit",
                        project_id,
                        member=self.name,
                        intent=intent_id,
                        steps=step,
                    )
                    return SolveResult(status="stalled", steps=step)
            else:
                invalid_actions = 0
            if dispatched.result is not None:
                # Terminal graph actions (flag/conclude) may be dispatched by
                # a custom provider bridge, so release the intent here even
                # when the dispatcher itself did not perform the cleanup.
                self._release(project_id, intent_id)
                return dispatched.result
        if self._stop.is_set():
            self._release(project_id, intent_id)
            d.logger.project("member_stopped", project_id, member=self.name, intent=intent_id, steps=step)
            return SolveResult(status="stopped", steps=step)

        if not graph_actions:
            self._submit_stall_report(project_id, intent_id, category, step)
        self._release(project_id, intent_id)
        status = "done" if graph_actions else "stalled"
        d.logger.project(
            "member_task_finished",
            project_id,
            member=self.name,
            intent=intent_id,
            status=status,
            steps=step,
            graph_actions=graph_actions,
        )
        return SolveResult(status=status, steps=step)

    def _native_competition_enabled(self) -> bool:
        config = getattr(self.adapter, "config", None)
        return bool(
            self.deps.competition_native
            and self.deps.competition_store is not None
            and self.deps.competition_assignment is not None
            and config is not None
            and getattr(config, "api_format", "mock") != "mock"
        )

    @staticmethod
    def _native_action_tool() -> dict[str, Any]:
        return {
            "name": "member_action",
            "description": (
                "Execute one CTF investigation action. Return the action kind "
                "and its fields; never claim a flag without evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTION_KINDS)},
                    "thought": {"type": "string"},
                    "args": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["action"],
                "additionalProperties": True,
            },
        }

    def _make_native_runner(self, tools: list[dict[str, Any]]) -> SessionRunner:
        factory = self.deps.competition_conversation_factory
        if factory is None:
            adapter = ConversationAdapter(self.adapter.config)
        else:
            adapter = factory(self.adapter.config)
        runner = SessionRunner(
            self.deps.competition_store,
            adapter,
            None,  # installed by _solve_with_native_session below
            assignment=self.deps.competition_assignment,
            compressor=compact_native_history,
            system=(
                f"You are Member {self.name}, a persistent {self.role_blurb}. "
                "Use the member_action function for every investigation step. "
                "Continue until the challenge is solved, a real flag is verified, "
                "or evidence is genuinely exhausted."
            ),
            tools=tools,
            cancel=self._stop,
            emit=lambda text: self.deps.logger.llm(
                "stream", self._progress_project_id or "unknown",
                member=self.name, text=text[:2000],
            ),
            before_turn=self._drain_competition_recon,
        )
        return runner

    async def _solve_with_native_session(
        self,
        project_id: str,
        intent_id: str,
        category: str,
        is_initial: bool,
        mcp_session: MCPRegistrySession,
    ) -> SolveResult:
        """Drive a real provider tool-call stream through the durable runner.

        The legacy graph dispatcher remains the side-effect implementation. A
        provider-native call is translated into the same ``MemberAction`` and
        passes through the same lease/flag/WP transactions, so changing the
        conversation surface cannot bypass existing fencing rules.
        """
        if not self._claim(project_id, intent_id):
            self.deps.logger.project(
                "intent_claim_lost", project_id, member=self.name, intent=intent_id
            )
            return SolveResult(status="stalled", steps=0, error="intent lease unavailable")
        self._seed_tool_inventory(project_id)
        self._prime_tool_context(project_id, intent_id, category)
        state: dict[str, Any] = {
            "steps": 0,
            "graph_actions": [],
            "branch_intents": 0,
            "result": None,
            "error": None,
        }
        tool = self._native_action_tool()
        try:
            runner = self._make_native_runner([tool])
        except Exception as exc:
            self._release(project_id, intent_id)
            return SolveResult(
                status="failed", steps=0, error=str(exc),
                retryable=False, error_kind="native_setup",
            )
        member = self

        class Executor:
            async def execute(_self, name, arguments, *, idempotency_key):
                del idempotency_key
                if name != "member_action":
                    state["error"] = f"unsupported provider tool: {name}"
                    runner.cancel.set()
                    return {"status": "rejected", "error": state["error"]}
                raw = dict(arguments or {})
                nested = raw.pop("args", None)
                if isinstance(nested, dict):
                    merged = dict(nested)
                    merged.update(raw)
                    raw = merged
                try:
                    action = MemberAction.from_obj(raw)
                except (TypeError, ValueError) as exc:
                    state["error"] = str(exc)
                    runner.cancel.set()
                    return {"status": "invalid_action", "error": str(exc)}
                state["steps"] += 1
                step = state["steps"]
                if not member._heartbeat(project_id, intent_id):
                    state["error"] = "intent lease lost"
                    runner.cancel.set()
                    return {"status": "lease_lost"}
                loop_status = member._record_action_signature(action)
                if loop_status == "warn":
                    member._observe(
                        "[stuckness] repeated native action; switch exploit class or evidence source"
                    )
                if loop_status == "break":
                    state["error"] = "repeated provider action signature"
                    runner.cancel.set()
                    return {"status": "loop_detected"}
                member.deps.logger.llm(
                    "native_action", project_id, member=member.name,
                    intent=intent_id, step=step, action=action.kind,
                )
                try:
                    dispatched = await member._dispatch(
                        project_id,
                        intent_id,
                        category,
                        action,
                        step,
                        mcp_session,
                        allow_intent=state["branch_intents"] < 1,
                    )
                except FlagConflictError as exc:
                    state["error"] = str(exc)
                    state["result"] = SolveResult(
                        status="stalled", steps=step, error=str(exc),
                        retryable=False, error_kind="flag_conflict",
                    )
                    runner.cancel.set()
                    return {"status": "flag_conflict", "error": str(exc)}
                except ValueError as exc:
                    state["error"] = str(exc)
                    state["result"] = SolveResult(
                        status="stalled", steps=step, error=str(exc),
                        retryable=False, error_kind="terminal_validation",
                    )
                    runner.cancel.set()
                    return {"status": "validation_error", "error": str(exc)}
                if dispatched.graph_action is not None:
                    state["graph_actions"].append(dispatched.graph_action)
                    if dispatched.graph_action == "intent":
                        state["branch_intents"] += 1
                result = dispatched.result
                if result is not None:
                    state["result"] = result
                    runner.cancel.set()
                payload: dict[str, Any] = {
                    "status": "ok",
                    "graph_action": dispatched.graph_action,
                    "invalid_action": dispatched.invalid_action,
                    "terminal": result.status if result else None,
                }
                if dispatched.observation:
                    payload["observation"] = dispatched.observation
                return payload

        runner.executor = Executor()
        try:
            context = self._build_context(
                project_id, intent_id, category, 0, is_initial, True
            )
            if is_initial:
                initial = (
                    f"Begin the {category} challenge for intent {intent_id}. "
                    "Persist useful evidence and choose the next action.\n"
                    + json.dumps(context, ensure_ascii=False)
                )
            else:
                initial = (
                    f"Continue the {category} challenge for intent {intent_id}. "
                    "A previous attempt ended without a verified flag: do not "
                    "repeat an earlier final answer or candidate flag; choose a "
                    "different concrete analysis or exploit action now.\n"
                    + json.dumps(context, ensure_ascii=False)
                )
                # A restored session ends with the previous attempt's
                # conclusion, and the runner does not re-add the bootstrap
                # prompt when history exists.  Record a durable continuation
                # directive so the model does not just replay that final
                # answer.  The payload is intentionally stable per intent: a
                # retry must reuse the stored copy instead of failing with an
                # event-key conflict.
                try:
                    runner.record_user_message(
                        (
                            f"New assigned intent {intent_id}. The previous attempt "
                            "ended without a verified flag. Do not repeat an earlier "
                            "final answer or candidate flag; choose a different "
                            "concrete analysis or exploit action now with the "
                            "member_action tool."
                        ),
                        event_key=f"session:user:{intent_id}",
                    )
                except CompetitionConflict:
                    # The directive is already durable for this intent; the
                    # model sees the stored copy on the restored history.
                    pass
            native_result = await runner.run_async(initial)
        except ProviderError as exc:
            self._release(project_id, intent_id)
            self.deps.logger.project(
                "member_error", project_id, member=self.name, intent=intent_id,
                error=f"{type(exc).__name__}: {exc}", retryable=exc.retryable,
                error_kind="provider",
            )
            return SolveResult(
                status="failed", steps=state["steps"], error=str(exc),
                retryable=exc.retryable, error_kind="provider",
            )
        except Exception as exc:
            self._release(project_id, intent_id)
            self.deps.logger.project(
                "member_error", project_id, member=self.name, intent=intent_id,
                error=f"{type(exc).__name__}: {exc}", retryable=True,
                error_kind="native_runtime",
            )
            return SolveResult(
                status="failed", steps=state["steps"], error=str(exc),
                retryable=True, error_kind="native_runtime",
            )
        if state["result"] is not None:
            # The provider stream cancels immediately after a terminal
            # MemberAction.  Release the durable intent before returning the
            # result so a replacement/helper can be scheduled safely.
            self._release(project_id, intent_id)
            return state["result"]
        if state["error"]:
            self._release(project_id, intent_id)
            return SolveResult(
                status="stalled", steps=state["steps"], error=state["error"],
            )
        if self._stop.is_set():
            self._release(project_id, intent_id)
            return SolveResult(status="stopped", steps=state["steps"])
        status = "done" if state["graph_actions"] or native_result.status == "idle" else "stalled"
        if state["error"]:
            status = "stalled"
        if status == "done" and not state["graph_actions"]:
            # No graph change and no further actions.  Close the assigned
            # intent so the coordinator can plan a new direction instead of
            # redispatching the same finished intent forever.
            self._conclude_with_description(
                project_id,
                intent_id,
                "member reported no further actions for this intent",
            )
        self._release(project_id, intent_id)
        self.deps.logger.project(
            "member_task_finished", project_id, member=self.name,
            intent=intent_id, status=status, steps=state["steps"],
            runtime="provider-native",
        )
        return SolveResult(
            status=status, steps=state["steps"], error=state["error"]
        )

    async def _dispatch(
        self,
        project_id,
        intent_id,
        category,
        action: MemberAction,
        step,
        mcp_session: MCPRegistrySession,
        *,
        allow_intent: bool,
    ) -> DispatchResult:
        d = self.deps
        kind = action.kind
        if kind == "bash":
            cmd = self._string_arg(action.args.get("command", ""))
            if not cmd.strip():
                d.logger.project(
                    "invalid_bash_action",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    keys=sorted(action.args),
                )
                text = "[invalid bash action omitted: missing non-empty `command`]"
                self._observe(text)
                return DispatchResult(invalid_action=True, observation=text)
            res = d.sandbox.exec(cmd, timeout=60)
            text = f"$ {cmd}\n{res.stdout}\n{res.stderr}".strip()
            self._observe(text)
            self._observe_webui_links(project_id, cmd, res.stdout, res.stderr)
            d.logger.tool(
                "bash",
                project_id,
                member=self.name,
                command=cmd,
                exit_code=res.exit_code,
                stdout=res.stdout[:4000],
                stderr=res.stderr[:4000],
            )
            missing_command = self._missing_command_from_result(cmd, res.stderr)
            if missing_command:
                knowledge = self._record_unavailable_cli(project_id, intent_id, missing_command)
                return DispatchResult(
                    invalid_action=True, invalid_knowledge=knowledge, observation=text
                )
            return DispatchResult(observation=text)
        if kind == "tool":
            server = self._string_arg(action.args.get("server", ""))
            tool = self._string_arg(action.args.get("tool", ""))
            args = action.args.get("args", {})
            if not isinstance(args, dict):
                args = {}
            if not str(server).strip() or not str(tool).strip():
                d.logger.project(
                    "invalid_tool_action",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    keys=sorted(action.args),
                )
                text = "[invalid tool action omitted: missing `server` or `tool`]"
                self._observe(text)
                return DispatchResult(invalid_action=True, observation=text)
            try:
                out = await mcp_session.call_tool(server, tool, args)
            except Exception as exc:
                out = {"error": str(exc)}
            text = f"[mcp:{server}.{tool}] {out}"[:5000]
            self._observe(text)
            artifact = out if isinstance(out, dict) and out.get("artifact_id") else {}
            sensitive_operation = bool(
                server == "browser"
                and (
                    (tool == "cookies" and args.get("include_values") is True)
                    or tool == "set_cookie"
                )
            )
            d.logger.tool(
                "mcp_call",
                project_id,
                member=self.name,
                server=server,
                tool=tool,
                sensitive_operation=sensitive_operation,
                artifact_id=artifact.get("artifact_id"),
                relative_path=artifact.get("relative_path"),
                artifact_size=artifact.get("size"),
                artifact_sha256=artifact.get("sha256"),
            )
            return DispatchResult(observation=text)
        if kind == "memory":
            query = self._string_arg(action.args.get("query", ""))
            hits = mem_search(d.memory, query, limit=5)
            text = f"[memory:{query}] " + "; ".join(f"{m.title}" for m, _ in hits)
            self._observe(text)
            d.logger.memory("search", project_id, member=self.name, query=query, hits=len(hits))
            return DispatchResult(observation=text)
        if kind == "tool_search":
            query = self._string_arg(action.args.get("query", ""))
            tools = d.registry.search(
                query, available_mcps=self._available_mcp_names()
            )
            text = f"[tool_search:{query}] " + ", ".join(t.name for t in tools)
            self._observe(text)
            d.logger.tool("tool_search", project_id, member=self.name, query=query)
            return DispatchResult(observation=text)
        if kind == "report":
            report = self._submit_report(project_id, intent_id, action)
            return DispatchResult(graph_action="report" if report is not None else None)
        if kind == "intent":
            if not allow_intent:
                d.logger.project(
                    "intent_budget_exhausted",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    description=action.args.get("description", "explore"),
                )
                return DispatchResult()
            created = self._declare_intent(project_id, intent_id, action)
            return DispatchResult(graph_action="intent" if created is not None else None)
        if kind == "conclude":
            return DispatchResult(result=self._conclude(project_id, intent_id, action), graph_action="conclude")
        if kind == "flag":
            return DispatchResult(result=self._raise_flag(project_id, intent_id, action, step), graph_action="flag")
        if kind == "done":
            reason = self._string_arg(action.args.get("reason", "member gave up")) or "member gave up"
            concluded = self._conclude_with_description(
                project_id, intent_id, f"member stopped: {reason}"[:500]
            )
            self._release(project_id, intent_id)
            d.logger.project("member_done", project_id, member=self.name, reason=reason)
            return DispatchResult(
                result=SolveResult(status="done", steps=step),
                graph_action="conclude" if concluded.status == "concluded" else None,
            )
        return DispatchResult()

    def _claim(self, project_id, intent_id) -> bool:
        # Standalone Members do not receive an orchestrator assignment.  The
        # claim API uses the worker name as the default lease owner; retain that
        # owner locally so subsequent heartbeat/conclude/release calls carry a
        # complete fencing tuple.
        if self.deps.lease_owner is None:
            self.deps.lease_owner = self.name
        with self.deps.db.connect() as conn:
            token = edge_store.claim_intent(
                conn,
                project_id,
                intent_id,
                self.name,
                lease_owner=self.deps.lease_owner,
                lease_token=self.deps.lease_token,
            )
        if token:
            self.deps.lease_token = token
            return True
        return False

    def _heartbeat(self, project_id, intent_id) -> bool:
        """Renew the assigned intent and report whether this worker is fenced in.

        Returning ``False`` for a missing/concluded intent, an expired lease, or
        a database failure lets the solve loop stop before dispatching a model
        action.  The fencing token check lives in ``claim_intent``; merely
        seeing the same worker name is not sufficient because a replacement
        worker may have reclaimed the intent.
        """

        try:
            with self.deps.db.connect() as conn:
                row = edge_store.get_intent(conn, project_id, intent_id)
                if row is None or row["to_fact_id"] is not None:
                    return False
                token = edge_store.claim_intent(
                    conn,
                    project_id,
                    intent_id,
                    self.name,
                    lease_owner=self.deps.lease_owner,
                    lease_token=self.deps.lease_token,
                )
            if token:
                self.deps.lease_token = token
                return True
        except Exception as exc:
            self.deps.logger.project(
                "intent_heartbeat_failed",
                project_id,
                member=self.name,
                intent=intent_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        return False

    def _release(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is not None and row["to_fact_id"] is None and row["worker"] == self.name:
                edge_store.release_intent(
                    conn,
                    project_id,
                    intent_id,
                    lease_owner=self.deps.lease_owner,
                    lease_token=self.deps.lease_token,
                    worker=self.name,
                )
        # Never carry a stale fencing tuple into a later task.
        self.deps.lease_token = None

    def _restore_progress(self, project_id: str, intent_id: str) -> None:
        safe_intent = re.sub(r"[^A-Za-z0-9_.-]+", "_", intent_id).strip("._") or "intent"
        self._progress_path = f".ipc/progress/{safe_intent}.json"
        self._progress_project_id = project_id
        self._progress_save_error_reported = False
        self.observations = []
        try:
            raw = self.deps.sandbox.read_file(self._progress_path)
            parsed = json.loads(raw) if raw else []
            if not isinstance(parsed, list):
                raise ValueError("progress checkpoint must contain a JSON array")
            self.observations = [str(item)[:5000] for item in parsed if str(item).strip()][-8:]
        except Exception as exc:
            self.deps.logger.project(
                "member_progress_restore_failed",
                project_id,
                member=self.name,
                intent=intent_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.observations = []
            return
        if self.observations:
            self.deps.logger.project(
                "member_progress_restored",
                project_id,
                member=self.name,
                intent=intent_id,
                observations=len(self.observations),
            )

    def _persist_progress(self) -> None:
        if self._progress_path is None:
            return
        try:
            self.deps.sandbox.write_file(
                self._progress_path,
                json.dumps(self.observations[-8:], ensure_ascii=False),
            )
        except Exception as exc:
            if self._progress_save_error_reported:
                return
            self._progress_save_error_reported = True
            self.deps.logger.project(
                "member_progress_save_failed",
                self._progress_project_id or "system",
                member=self.name,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _seed_tool_inventory(self, project_id: str) -> None:
        try:
            self.deps.sandbox.write_file(
                "tools.txt",
                member_tool_inventory().rstrip() + "\n\n" + self._runtime_tool_inventory_note(),
            )
        except Exception as exc:
            self.deps.logger.project(
                "member_tool_inventory_seed_failed",
                project_id,
                member=self.name,
                error=str(exc),
            )

    def _prime_tool_context(self, project_id: str, intent_id: str, category: str) -> None:
        try:
            with self.deps.db.connect() as conn:
                row = edge_store.get_intent(conn, project_id, intent_id)
                goal = next(
                    (fact.description for fact in node_store.list_facts(conn, project_id) if fact.id == "goal"),
                    "",
                )
            intent_desc = row["description"] if row is not None else ""
            query = " ".join(part for part in (category, intent_desc, goal) if part).strip()
            if not query:
                return
            tools = self.deps.registry.search(
                query, available_mcps=self._available_mcp_names()
            )[:5]
        except Exception as exc:
            self.deps.logger.project(
                "member_tool_context_prime_failed",
                project_id,
                member=self.name,
                intent=intent_id,
                error=str(exc),
            )
            return
        summary = ", ".join(
            f"{tool.name}({tool.category})" for tool in tools
        ) or "no matching registered tools"
        self._observe(f"[tool_search:auto:{query[:120]}] {summary}")
        self.deps.logger.tool(
            "tool_search",
            project_id,
            member=self.name,
            query=query,
            hits=[tool.name for tool in tools],
            automatic=True,
        )

    def _probe_cli_tools(self) -> dict[str, bool]:
        if self._tool_availability is not None:
            return self._tool_availability
        names_json = json.dumps(list(_CLI_PROBE_TOOLS))
        code = (
            "import json, shutil; "
            f"names=json.loads('{names_json}'); "
            "print(json.dumps({n: bool(shutil.which(n)) for n in names}, sort_keys=True))"
        )
        command = f'python3 -c "{code}" || python -c "{code}"'
        availability: dict[str, bool] = {}
        try:
            res = self.deps.sandbox.exec(command, timeout=10)
            for line in reversed(res.stdout.splitlines()):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    availability = {str(k): bool(v) for k, v in parsed.items()}
                    break
        except Exception:
            availability = {}
        self._tool_availability = availability
        return availability

    def _runtime_tool_inventory_note(self) -> str:
        availability = self._probe_cli_tools()
        if not availability:
            return (
                "Runtime tool availability\n"
                "-------------------------\n"
                "- probe: unavailable; trust the static inventory only after checking commands with `command -v` or `which`."
            )
        available = ", ".join(name for name, ok in availability.items() if ok) or "none"
        missing = ", ".join(name for name, ok in availability.items() if not ok) or "none"
        return (
            "Runtime tool availability\n"
            "-------------------------\n"
            f"- available now: {available}\n"
            f"- missing now: {missing}\n"
            "- If a command is missing, switch to an available fallback or call tool_search/MCP instead of retrying it."
        )

    def _record_unavailable_cli(self, project_id: str, intent_id: str, command_name: str) -> list[str]:
        command_name = command_name.strip()
        if not command_name:
            return []
        availability = self._probe_cli_tools()
        availability[command_name] = False
        self._tool_availability = availability
        count = self._missing_tool_counts.get(command_name, 0) + 1
        self._missing_tool_counts[command_name] = count
        self._observe(
            f"[tool unavailable:{command_name}] This command is missing in the sandbox. "
            "Use `cat tools.txt` to choose an installed fallback, or call tool_search/MCP."
        )
        self.deps.logger.project(
            "sandbox_tool_unavailable",
            project_id,
            member=self.name,
            intent=intent_id,
            command=command_name,
            count=count,
        )
        return [f"unavailable_cli_tool:{command_name}"]

    def _missing_command_from_result(self, command: str, stderr: str) -> str | None:
        text = stderr or ""
        for match in _WINDOWS_COMMAND_NOT_FOUND_RE.finditer(text):
            return match.group(1)
        for match in _COMMAND_NOT_FOUND_RE.finditer(text):
            candidate = match.group(1)
            if candidate not in {"line", "bash", "sh"}:
                return candidate
        if "not found" in text.lower() or "command not found" in text.lower():
            first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
            return first or None
        return None

    def _submit_report(self, project_id, intent_id, action: MemberAction):
        d = self.deps
        a = dict(action.args)
        with d.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            node_id = row["to_fact_id"] if row else None
            if node_id is None and row is not None:
                sources = self._intent_source_ids(conn, project_id, intent_id)
                node_id = sources[-1] if sources else None
            progress = self._string_arg(a.get("progress", ""))
            steps = self._list_arg(a.get("steps", []))
            directions = self._list_arg(a.get("directions", []))
            knowledge = self._list_arg(a.get("knowledge", []))
            intent_tag = f"intent:{intent_id}"
            if intent_tag not in knowledge:
                knowledge.append(intent_tag)
            difficulty, evidence = self._calibrate_difficulty(
                conn,
                project_id,
                intent_id,
                node_id,
                progress,
                self._string_arg(a.get("difficulty", "low")) or "low",
                steps,
                directions,
                knowledge,
            )
            for item in evidence:
                tag = f"evidence:{item}"
                if tag not in knowledge:
                    knowledge.append(tag)
            if self._should_suppress_report(conn, project_id, intent_id, node_id, difficulty, evidence):
                d.logger.project(
                    "difficulty_report_suppressed",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    difficulty=difficulty,
                    reason="unchanged_difficulty",
                )
                return None
            report = graph_store.create_report(
                conn, project_id, self.name, progress, difficulty,
                node_id, steps, directions, knowledge,
            )
            graph_store.add_link(conn, project_id, self.name, "diamond", "report")
        d.logger.project(
            "difficulty_report",
            project_id,
            member=self.name,
            difficulty=report.difficulty,
            evidence=evidence,
        )
        if d.on_report is not None:
            d.on_report(project_id, report)
        return report

    def _list_arg(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    def _calibrate_difficulty(
        self,
        conn,
        project_id: str,
        intent_id: str,
        node_id: str | None,
        progress: str,
        requested: str,
        steps: list[str],
        directions: list[str],
        knowledge: list[str],
    ) -> tuple[str, list[str]]:
        level = normalize_difficulty(requested)
        evidence: list[str] = []
        reports = graph_store.list_reports(conn, project_id)
        intent_tag = f"intent:{intent_id}"
        scoped = [
            report for report in reports
            if intent_tag in report.knowledge or (node_id is not None and report.node_id == node_id)
        ]

        if "short_task_stall" in knowledge or "no_new_fact" in knowledge:
            prior_no_fact = sum(
                1 for report in scoped
                if "short_task_stall" in report.knowledge or "no_new_fact" in report.knowledge
            )
            no_fact_count = prior_no_fact + 1
            if no_fact_count >= 3:
                level = max_difficulty(level, "high")
                evidence.append(f"no_new_fact_short_tasks:{no_fact_count}")
            elif no_fact_count >= 2:
                level = max_difficulty(level, "medium")
                evidence.append(f"no_new_fact_short_tasks:{no_fact_count}")

        if "action_signature_repeat" in knowledge:
            prior_repeats = sum(1 for report in scoped if "action_signature_repeat" in report.knowledge)
            level = max_difficulty(level, "high" if prior_repeats else "medium")
            evidence.append("action_signature_repeat")

        # Calibrate from observed evidence, not speculative next-step phrasing.
        texts = [progress, *steps, *knowledge]
        for report in scoped[-5:]:
            texts.extend([report.progress, *report.steps, *report.knowledge])
        exploit_classes = detect_exploit_classes(texts)
        if len(exploit_classes) >= 4:
            level = max_difficulty(level, "ex")
            evidence.append("distinct_exploit_classes:4+")
        elif len(exploit_classes) >= 3:
            level = max_difficulty(level, "high")
            evidence.append("distinct_exploit_classes:3")
        elif len(exploit_classes) >= 2:
            level = max_difficulty(level, "medium")
            evidence.append("distinct_exploit_classes:2")

        surface_texts = list(texts)
        surface_texts.extend(f.description for f in node_store.list_facts(conn, project_id))
        surface_texts.extend(h.content for h in graph_store.list_hints(conn, project_id))
        surface_texts.extend(a.filename for a in graph_store.list_attachments(conn, project_id))
        surfaces = detect_attack_surfaces(surface_texts)
        if len(surfaces) >= 4:
            level = max_difficulty(level, "ex")
            evidence.append("credible_attack_surfaces:4+")
        elif len(surfaces) >= 3:
            level = max_difficulty(level, "high")
            evidence.append("credible_attack_surfaces:3")
        elif len(surfaces) >= 2:
            level = max_difficulty(level, "medium")
            evidence.append("credible_attack_surfaces:2")

        if (
            DIFFICULTY_RANK[level] >= DIFFICULTY_RANK["high"]
            and len(exploit_classes) >= 2
            and len(surfaces) >= 2
            and any(item.startswith("no_new_fact_short_tasks:") for item in evidence)
        ):
            level = max_difficulty(level, "ex")
            evidence.append("combined_stuckness")

        return level, evidence

    def _should_suppress_report(
        self,
        conn,
        project_id: str,
        intent_id: str,
        node_id: str | None,
        difficulty: str,
        evidence: list[str],
    ) -> bool:
        intent_tag = f"intent:{intent_id}"
        reports = [
            report for report in graph_store.list_reports(conn, project_id)
            if report.member == self.name
            and (intent_tag in report.knowledge or (node_id is not None and report.node_id == node_id))
        ]
        if not reports:
            return False
        latest = reports[-1]
        if normalize_difficulty(latest.difficulty) != normalize_difficulty(difficulty):
            return False
        latest_evidence = {
            item.removeprefix("evidence:")
            for item in latest.knowledge
            if item.startswith("evidence:")
        }
        new_evidence = [item for item in evidence if item not in latest_evidence]
        return not new_evidence

    def _declare_intent(self, project_id, current_intent_id, action: MemberAction):
        a = action.args
        requested_from = a.get("from")
        description = self._string_arg(a.get("description", "explore")) or "explore"
        with self.deps.db.connect() as conn:
            if requested_from:
                from_ids = self._list_arg(requested_from)
                for fid in from_ids:
                    if not node_store.fact_exists(conn, project_id, fid):
                        from_ids = ["origin"]
                        break
            else:
                from_ids = self._default_intent_sources(conn, project_id, current_intent_id)
            existing = edge_store.find_similar_open_intent(conn, project_id, from_ids, description)
            if existing is not None:
                self.deps.logger.project(
                    "intent_deduped",
                    project_id,
                    member=self.name,
                    existing_intent=existing.id,
                    from_ids=from_ids,
                    description=description,
                )
                return None
            intent = edge_store.create_intent(conn, project_id, from_ids, description, self.name)
            graph_store.add_link(conn, project_id, self.name, f"intent:{intent.id}", "explore")
            self.deps.logger.project(
                "intent_declared",
                project_id,
                member=self.name,
                intent=intent.id,
                from_ids=from_ids,
                description=description,
            )
            return intent

    def _intent_source_ids(self, conn, project_id, intent_id) -> list[str]:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = %s AND project_id = %s ORDER BY fact_id",
            (intent_id, project_id),
        ).fetchall()
        return [row["fact_id"] for row in rows]

    def _default_intent_sources(self, conn, project_id, intent_id) -> list[str]:
        current_sources = [
            fact_id
            for fact_id in self._intent_source_ids(conn, project_id, intent_id)
            if node_store.fact_exists(conn, project_id, fact_id)
        ]
        non_root_sources = [fact_id for fact_id in current_sources if fact_id not in ("origin", "goal")]
        if non_root_sources:
            return non_root_sources

        facts = [fact.id for fact in node_store.list_facts(conn, project_id) if fact.id not in ("origin", "goal")]
        if facts:
            return [facts[-1]]
        return current_sources or ["origin"]

    def _submit_stall_report(
        self,
        project_id,
        intent_id,
        category,
        step,
        *,
        difficulty_hint: str = "low",
        extra_knowledge: list[str] | None = None,
    ) -> None:
        recent = self.observations[-4:]
        progress = "Short exploration ended without a confirmed result."
        if recent:
            progress = "Short exploration observations:\n" + "\n\n".join(recent)
        knowledge = [category, "short_task_stall", "no_new_fact", *(extra_knowledge or [])]
        action = MemberAction(
            kind="report",
            thought="short task budget exhausted; sharing observations for follow-up",
            args={
                "progress": progress[:1800],
                "difficulty": difficulty_hint,
                "steps": recent or [f"Used {step} short-task actions on {category} intent."],
                "directions": [
                    "Try a different concrete approach for this intent.",
                    "Use sibling findings and avoid repeating the same command sequence.",
                ],
                "knowledge": knowledge,
            },
        )
        self._submit_report(project_id, intent_id, action)
        self.deps.logger.project(
            "member_stalled",
            project_id,
            member=self.name,
            intent=intent_id,
            steps=step,
        )

    def _record_action_signature(self, action: MemberAction) -> str | None:
        if action.kind not in {"bash", "tool", "tool_search", "memory"}:
            return None
        sig = action.kind + ":" + json.dumps(action.args, sort_keys=True, ensure_ascii=False)
        self._recent_action_sigs.append(sig)
        count = sum(1 for item in self._recent_action_sigs if item == sig)
        if count >= 5:
            return "break"
        if count >= 3:
            return "warn"
        return None

    def _reset_action_signatures(self) -> None:
        self._recent_action_sigs.clear()

    def bump(self, insights: str) -> None:
        text = insights.strip()
        if not text:
            return
        with self._state_lock:
            self._pending_bumps.append(text[:1800])
            self._pending_bumps = self._pending_bumps[-5:]
        self._reset_action_signatures()

    def _consume_bumps(self) -> list[str]:
        with self._state_lock:
            bumps = list(self._pending_bumps)
            self._pending_bumps.clear()
        return bumps

    def _conclude(self, project_id, intent_id, action: MemberAction) -> SolveResult:
        desc = self._string_arg(action.args.get("description", "confirmed result")) or "confirmed result"
        return self._conclude_with_description(project_id, intent_id, desc)

    def _conclude_with_description(
        self, project_id, intent_id, desc: str
    ) -> SolveResult:
        try:
            with self.deps.db.connect() as conn:
                row = edge_store.get_intent(conn, project_id, intent_id)
                if row is None or row["to_fact_id"] is not None:
                    return SolveResult(status="done", steps=0)
                fact = node_store.reserve_fact(conn, project_id, desc)
                concluded = edge_store.conclude_intent(
                    conn,
                    project_id,
                    intent_id,
                    self.name,
                    fact.id,
                    lease_owner=self.deps.lease_owner,
                    lease_token=self.deps.lease_token,
                )
                if not concluded:
                    # Returning here would commit ``fact`` because the
                    # connection context sees a normal exit. Raise so the
                    # transaction rolls back both the fact and its counter.
                    raise _IntentLeaseLost
                node_store.insert_fact(conn, project_id, fact.id, fact.description)
                graph_store.add_link(conn, project_id, self.name, f"fact:{fact.id}", "explore")
                graph_store.touch_project(conn, project_id)
        except _IntentLeaseLost:
            return SolveResult(status="stalled", steps=0, error="intent lease lost")
        self.deps.logger.project("intent_concluded", project_id, member=self.name, intent=intent_id, fact=fact.id)
        return SolveResult(status="concluded", steps=0, fact_id=fact.id)

    def _raise_flag(self, project_id, intent_id, action: MemberAction, step) -> SolveResult:
        d = self.deps
        flag = self._string_arg(action.args.get("flag", ""))
        desc = self._string_arg(action.args.get("description", "flag captured")) or "flag captured"
        try:
            with d.db.connect() as conn:
                row = edge_store.get_intent(conn, project_id, intent_id)
                if row is None:
                    return SolveResult(status="stalled", steps=step, error="intent not found")
                # A concluded intent has no live fencing token.  Treat a
                # replayed flag action as stale instead of letting it reuse a
                # historical fact and write a second completion/flag edge.
                if row["to_fact_id"] is not None:
                    raise _IntentLeaseLost
                # ensure the assigned intent is concluded into a fact first
                if row["to_fact_id"] is None:
                    fact = node_store.reserve_fact(conn, project_id, desc)
                    concluded = edge_store.conclude_intent(
                        conn,
                        project_id,
                        intent_id,
                        self.name,
                        fact.id,
                        lease_owner=self.deps.lease_owner,
                        lease_token=self.deps.lease_token,
                    )
                    if not concluded:
                        raise _IntentLeaseLost
                    node_store.insert_fact(conn, project_id, fact.id, fact.description)
                    graph_store.add_link(conn, project_id, self.name, f"fact:{fact.id}", "explore")
                    from_fact = fact.id
                else:
                    from_fact = row["to_fact_id"]
                # completion edge -> goal; project-row locking makes this idempotent
                # when multiple Members report the same Flag concurrently.
                edge_store.complete_goal_intent(
                    conn, project_id, [from_fact], desc, self.name, self.name
                )
                # Platform-linked projects only queue the candidate here; the
                # orchestrator's verdict worker accepts it once the platform
                # judge confirms.  Local projects are accepted immediately.
                candidate = submit_flag_candidate(
                    conn,
                    project_id,
                    flag,
                    source=f"member:{self.name}",
                )
                if candidate["mode"] == "verified":
                    enqueue_postprocess(conn, project_id)
                graph_store.add_link(conn, project_id, f"fact:{from_fact}", "flag", "flag")
        except _IntentLeaseLost:
            return SolveResult(status="stalled", steps=step, error="intent lease lost")
        d.logger.project("flag_found", project_id, member=self.name, flag=flag)
        if d.on_flag is not None:
            d.on_flag(project_id)
        return SolveResult(status="flag", steps=step, flag=flag, fact_id=from_fact)

    # ---- context ----

    def _available_mcp_names(self) -> list[str]:
        return list(
            dict.fromkeys(
                [*self.deps.mcps.names(), *(self.deps.container_mcps or {})]
            )
        )

    def _build_context(self, project_id, intent_id, category, step, is_initial, evaluate_now) -> dict:
        d = self.deps
        with d.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            raise RuntimeError(f"project {project_id} not found")
        assigned = next((i for i in detail.intents if i.id == intent_id), None)
        available_mcps = self._available_mcp_names()
        exposed = [
            tool.to_dict()
            for tool in d.registry.exposed_for(
                category, available_mcps=available_mcps
            )
        ]
        cli_availability = self._probe_cli_tools()
        reports = detail.reports[-8:]
        sibling_insights = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
                "knowledge": r.knowledge,
            }
            for r in reports
            if r.member != self.name
        ]
        pending_bumps = self._consume_bumps()
        previous_attempts = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
            }
            for r in reports
            if r.member == self.name or (assigned and r.node_id in (None, assigned.to))
        ]
        attachments = [
            {"id": a.id, "filename": a.filename, "path": a.path, "created_at": a.created_at}
            for a in detail.attachments
        ]
        attachment_true = bool(attachments)
        attachment_path = getattr(d.sandbox, "visible_attachment_path", None)
        if callable(attachment_path):
            for attachment in attachments:
                attachment["path"] = attachment_path(attachment["filename"], attachment["path"])
        runtime_notes = [
            "If sandbox_backend is LocalSandbox, use host shell-compatible commands only.",
            "If sandbox_backend is MemberSandbox, use Linux commands inside the shared task container.",
            "Flag search priority for this round: try /flag first, then environment variables, then other methods.",
            "If attachment_true is true, inspect the listed attachments before blind target probing.",
            (
                "challenge_description contains the original challenge statement "
                "(connection commands like nc/ssh, service addresses, flag format). "
                "external_id is this challenge's platform id — use it with ret2shell MCP tools."
            ),
        ]
        # Platform verdicts flow back through the experience memory: surface
        # any flags the platform already rejected for THIS challenge so the
        # member never resubmits them and re-derives a complete flag instead.
        try:
            rejected_flags: list[str] = []
            for m in d.memory.list(None):
                if m.project_id != project_id or "rejected-flag" not in m.tags:
                    continue
                rejected_flags.extend(re.findall(r"'([^']+)'", m.content))
            if rejected_flags:
                runtime_notes.append(
                    "Platform already REJECTED these flag strings for this challenge — "
                    f"do NOT resubmit them: {', '.join(sorted(set(rejected_flags)))}. "
                    "Re-derive the complete flag from the challenge material (check every "
                    "file/resource, including binary XML and encoded blobs; recombine ALL "
                    "fragments) and only report a flag that reads as a coherent whole."
                )
        except Exception:
            pass
        return {
            "role": self.name,
            "role_blurb": self.role_blurb,
            "category": category,
            "step": step,
            "max_steps": None if d.continuous else min(d.max_steps, d.max_actions_per_task),
            "short_task": not d.continuous,
            "task_contract": (
                "Continue this challenge session until there is a verified result, an operator cancellation, "
                "a lost lease, or concrete evidence that a different direction or helper is required. "
                "There is no artificial step budget. Do not repeat previous attempts."
                if d.continuous else
                "This is a short exploration task. Produce one clear result quickly: "
                "flag, conclude, a useful new intent, or a difficulty report with concrete next directions. "
                "Evaluate difficulty every eval_interval steps, but report only when the assessed level changes "
                "or new evidence justifies escalation. Do not repeat previous attempts."
            ),
            "sandbox_backend": getattr(d.sandbox, "__class__", type(d.sandbox)).__name__,
            "runtime_notes": runtime_notes,
            "evaluate_now": evaluate_now,
            "eval_interval": d.eval_interval,
            "is_initial": is_initial,
            "expected_flag": d.expected_flag,
            "challenge_description": next(
                (f.description for f in detail.facts if f.id == "origin"), ""
            ),
            "external_id": detail.project.external_id,
            "goal": next((f.description for f in detail.facts if f.id == "goal"), ""),
            "assigned_intent": {"id": intent_id, "description": assigned.description if assigned else ""},
            "assigned_intent_sources": assigned.from_ if assigned else ["origin"],
            "latest_fact_id": detail.facts[-1].id if detail.facts else "origin",
            "facts": [{"id": f.id, "description": f.description} for f in detail.facts],
            "hints": [
                {"id": h.id, "content": h.content, "creator": h.creator, "created_at": h.created_at}
                for h in detail.hints
            ],
            "open_intents": [
                {"id": i.id, "description": i.description}
                for i in detail.intents if i.to is None
            ],
            "exposed_tools": exposed,
            "member_tool_inventory": member_tool_inventory(),
            "member_tool_inventory_source": {
                "backend_path": member_tool_inventory_path(),
                "workspace_path": "tools.txt",
                "docker_path": "/tools.txt",
                "note": "Use bash `cat tools.txt`; in Docker sandboxes, `/tools.txt` is also available.",
            },
            "runtime_cli_tools": {
                "available": [name for name, ok in cli_availability.items() if ok],
                "missing": [name for name, ok in cli_availability.items() if not ok],
            },
            "available_mcps": available_mcps,
            "public_mcps": [
                name for name in PUBLIC_MCPS if name in available_mcps
            ],
            "available_languages": list(LANGUAGES),
            "attachment_true": attachment_true,
            "attachments": attachments,
            "recent_observations": self.observations[-6:],
            "stuckness_state": {
                "recent_action_signatures": len(self._recent_action_sigs),
                "loop_window": 12,
                "warn_threshold": 3,
                "break_threshold": 5,
            },
            "pending_bumps": pending_bumps,
            "bump_insights": sibling_insights,
            "previous_attempts": previous_attempts[-5:],
        }

    def _observe_webui_links(self, project_id: str, command: str, stdout: str, stderr: str) -> None:
        expose = getattr(self.deps.sandbox, "expose_webui", None)
        if not callable(expose):
            return
        ports = self._discover_webui_ports(command, stdout, stderr)
        for port in sorted(ports):
            try:
                url = expose(project_id, self.name, port)
            except Exception:
                continue
            self._observe(
                f"[webui:{port}] Shared browser URL: {url}/ "
                f"(open with MCP browser.navigate or browser.screenshot)"
            )

    def _discover_webui_ports(self, command: str, stdout: str, stderr: str) -> set[int]:
        ports = {int(match.group(1)) for match in _LOCAL_WEBUI_URL_RE.finditer(f"{stdout}\n{stderr}\n{command}")}
        if ports:
            return {port for port in ports if 1 <= port <= 65535}
        if not _WEBUI_HINT_RE.search(command):
            return set()
        return {
            int(match.group(1))
            for match in _PORT_FLAG_RE.finditer(command)
            if 1 <= int(match.group(1)) <= 65535
        }

    def _observe(self, text: str) -> None:
        self.observations.append(text[:5000])
        self._persist_progress()

    def _string_arg(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)
