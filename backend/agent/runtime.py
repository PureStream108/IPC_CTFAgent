"""The single durable action loop shared by Ops, competition and projects.

The loop has no step budget: it ends when the provider stops asking for tools,
when cancellation is requested, when the writer loses its fence, or when the
provider repeats an identical tool batch (a real loop, as opposed to slow
progress). Everything it needs from storage arrives through a
:class:`SessionWriter`, which is what lets one implementation serve a
competition Member seat and an Ops chat turn without either knowing about the
other's lease model.

Durability rules the loop depends on:

* Every provider message, tool call and tool result is appended before the next
  step, keyed so a replay after a restart is idempotent.
* A tool call whose result never landed is repaired as ``unknown`` rather than
  silently retried, because the side effect may already have happened.
* Executors receive a stable idempotency key and are responsible for returning
  a prior result instead of repeating a side effect.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.agent.compaction import deterministic_summary
from backend.agent.context import (
    calibrate,
    compacted_history,
    estimate_tokens,
    should_compact,
)
from backend.competition.conversation import (
    CONTINUATION_INSTRUCTION,
    ToolCall,
    TurnTruncated,
)


class ToolExecutor(Protocol):
    def execute(
        self, name: str, arguments: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...


class SessionWriter(Protocol):
    """Durable transcript access, fenced however the host runtime requires."""

    @property
    def session_id(self) -> str: ...

    def append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict: ...

    def events(self, after: int, limit: int) -> list[dict]: ...

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None: ...


@dataclass(slots=True)
class SessionResult:
    status: str
    turns: int
    text: str = ""


class AgentSessionWriter:
    """Writer backed by :class:`backend.agent.session.AgentSessionStore`.

    Ownership is captured at construction. A handoff advances the epoch, so a
    writer built by the previous owner starts failing instead of interleaving
    its writes into a transcript another worker now owns.
    """

    def __init__(self, store, session_id: str, owner: str, epoch: int) -> None:
        self.store = store
        self._session_id = session_id
        self.owner = owner
        self.epoch = epoch

    @property
    def session_id(self) -> str:
        return self._session_id

    def append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        return self.store.append_event(
            self._session_id, event_key, kind, payload,
            owner=self.owner, epoch=self.epoch,
        )

    def events(self, after: int, limit: int) -> list[dict]:
        return self.store.events(self._session_id, after=after, limit=limit)

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.store.save_checkpoint(self._session_id, checkpoint)

    def record_usage(self, turn: int, status: str, usage: dict | None = None) -> None:
        self.store.start_turn(self._session_id, turn, self.owner, self.epoch)
        self.store.finish_turn(self._session_id, turn, status, usage=usage)

    def record_compaction(self, summary, *, token_estimate: int | None = None) -> None:
        """Store the summary as a projection over the events it replaces.

        ``up_to_sequence`` is the last event the summary covers; the events
        themselves stay in place, so the evidence chain is never rewritten.
        """
        events = self.store.events(self._session_id, after=0, limit=500)
        last_sequence = events[-1]["sequence"] if events else 0
        while len(events) == 500:
            events = self.store.events(
                self._session_id, after=last_sequence, limit=500
            )
            if events:
                last_sequence = events[-1]["sequence"]
        self.store.record_compaction(
            self._session_id,
            last_sequence,
            summary if isinstance(summary, dict) else {"text": str(summary)},
            token_estimate=token_estimate,
        )

    def save_calibration(self, calibration: float) -> None:
        session = self.store.session(self._session_id)
        budget = dict(session.get("token_budget") or {})
        budget["calibration_ratio"] = calibration
        self.store.save_token_budget(self._session_id, budget)

    def handoff(self, new_owner: str, *, reason: str = "member_handoff") -> dict:
        row = self.store.handoff(self._session_id, new_owner, reason=reason)
        self.owner = row["owner"]
        self.epoch = row["epoch"]
        return row


class AssignmentSessionWriter:
    """Writer fenced by a competition solver assignment.

    The assignment lease stays authoritative for competition work because it
    encodes rules the generic writer table does not model: run status, the
    challenge deadline and the WP seat deadline.
    """

    def __init__(self, store, assignment: dict) -> None:
        self.store = store
        self.assignment = assignment

    @property
    def session_id(self) -> str:
        return self.assignment["session_id"]

    def append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        return self.store.append_event(
            self.session_id, event_key, kind, payload,
            assignment_id=self.assignment["id"],
            owner=self.assignment["lease_owner"],
            epoch=self.assignment["epoch"],
        )

    def events(self, after: int, limit: int) -> list[dict]:
        return self.store.events(self.session_id, after=after, limit=limit)

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.store.save_checkpoint(self.assignment, checkpoint)


class AgentRuntime:
    """One provider-native tool loop, independent of who owns the session."""

    def __init__(
        self,
        writer: SessionWriter,
        adapter=None,
        executor: ToolExecutor | None = None,
        *,
        system: str,
        tools: list[dict[str, Any]],
        cancel: threading.Event | None = None,
        emit: Callable[[str], None] | None = None,
        compressor: Callable[[list[dict[str, Any]]], tuple[str, list[dict[str, Any]]]] | None = None,
        before_turn: Callable[[AgentRuntime], list[dict[str, Any]]] | None = None,
        context_bytes: int = 1_000_000,
        repeated_batch_limit: int = 8,
        truncation_retry_limit: int = 3,
        max_turns: int = 0,
        context_token_limit: int = 0,
        compaction_trigger_ratio: float = 0.75,
        calibration: float = 1.0,
        keep_recent_messages: int = 8,
    ) -> None:
        self.writer = writer
        self.adapter = adapter
        self.executor = executor
        self.system = system
        self.tools = tools
        self.cancel = cancel or threading.Event()
        self.emit = emit or (lambda _text: None)
        self.compressor = compressor
        self.before_turn = before_turn
        self.context_bytes = max(16_384, context_bytes)
        self.repeated_batch_limit = max(3, repeated_batch_limit)
        self.truncation_retry_limit = max(1, truncation_retry_limit)
        # 0 means "no ceiling": convergence comes from the provider finishing,
        # cancellation, the repeated-batch detector and the watchdog.
        self.max_turns = max(0, max_turns)
        # A token budget replaces the byte approximation. 0 disables
        # token-based compaction and leaves only the byte ceiling.
        self.context_token_limit = max(0, context_token_limit)
        self.compaction_trigger_ratio = compaction_trigger_ratio
        self.calibration = calibration
        self.keep_recent_messages = max(2, keep_recent_messages)
        self._completed_turns = 0
        self._last_estimate = 0

    @property
    def session_id(self) -> str:
        return self.writer.session_id

    def _append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        return self.writer.append(event_key, kind, payload)

    def _all_events(self) -> list[dict]:
        events: list[dict] = []
        after = 0
        while True:
            page = self.writer.events(after, 500)
            if not page:
                break
            events.extend(page)
            after = page[-1]["sequence"]
            if len(page) < 500:
                break
        return events

    def _history(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for event in self._all_events():
            if event["kind"] == "provider_messages":
                messages.extend(event["payload"].get("messages", []))
                if event["event_key"].startswith("provider:"):
                    with_turn = event["event_key"].partition(":")[2]
                    if with_turn.isdigit():
                        self._completed_turns = max(
                            self._completed_turns, int(with_turn)
                        )
            elif event["kind"] == "tool_result":
                messages.append(event["payload"]["message"])
            elif event["kind"] == "context_compacted":
                # Compaction replaces the transcript with its retained tail.
                # The summary describes what was dropped, so it has to lead the
                # rebuilt history or the session loses every earlier finding.
                messages = compacted_history(event["payload"])
        return messages

    def record_user_message(
        self, content: str, *, event_key: str = "session:user:initial"
    ) -> dict:
        return self._append(
            event_key, "provider_messages",
            {"messages": [{"role": "user", "content": content}]},
        )

    def record_provider_message(
        self, message: dict[str, Any], *, event_key: str, text: str = ""
    ) -> dict:
        return self._append(
            event_key, "provider_messages", {"messages": [message], "text": text}
        )

    def record_tool_call(self, call: ToolCall, *, event_key: str | None = None) -> dict:
        return self._append(
            event_key or f"tool-call:{call.id}", "tool_call",
            {"id": call.id, "name": call.name, "arguments": call.arguments},
        )

    def record_tool_result(
        self, call: ToolCall, message: dict[str, Any], *, event_key: str | None = None
    ) -> dict:
        return self._append(
            event_key or f"tool-result:{call.id}", "tool_result",
            {"id": call.id, "message": message},
        )

    def record_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.writer.save_checkpoint(checkpoint)

    def call_state(self, call_id: str) -> str | None:
        """Return ``pending``/``done`` for a persisted call, if present."""
        state: str | None = None
        for event in self._all_events():
            payload = event.get("payload") or {}
            if event["kind"] == "tool_call" and payload.get("id") == call_id:
                state = "pending"
            elif event["kind"] == "tool_result" and payload.get("id") == call_id:
                state = "done"
        return state

    def _needs_compaction(self, messages: list[dict[str, Any]]) -> tuple[bool, dict]:
        """Decide whether to compact, preferring the token budget over bytes."""
        estimate = estimate_tokens(messages, calibration=self.calibration)
        self._last_estimate = estimate
        if self.context_token_limit and should_compact(
            messages,
            limit=self.context_token_limit,
            trigger_ratio=self.compaction_trigger_ratio,
            calibration=self.calibration,
        ):
            return True, {
                "reason": "token budget",
                "tokens": estimate,
                "limit": self.context_token_limit,
                "calibration": round(self.calibration, 4),
            }
        encoded = len(
            json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()
        )
        if encoded > self.context_bytes:
            return True, {
                "reason": "byte ceiling",
                "bytes": encoded,
                "limit": self.context_bytes,
                "tokens": estimate,
            }
        return False, {"tokens": estimate}

    def _compact(self, messages: list[dict[str, Any]], turn: int) -> list[dict[str, Any]]:
        """Compact the projection when it approaches the context budget.

        Compaction is an optimisation, not a precondition for running: if the
        semantic compressor fails, this degrades to a deterministic projection
        and records the failure, instead of ending a task that was making
        progress.
        """
        needed, detail = self._needs_compaction(messages)
        if not needed:
            return messages

        summary: Any
        compacted: list[dict[str, Any]]
        if self.compressor is None:
            summary, compacted = deterministic_summary(
                messages, keep_recent=self.keep_recent_messages
            )
            self._append(
                f"compact-degraded:{turn}", "error",
                {**detail, "status": "no_compressor",
                 "note": "compacted deterministically; no semantic summarizer configured"},
            )
        else:
            try:
                summary, compacted = self.compressor(messages)
                if not compacted:
                    raise ValueError("context compressor returned no provider messages")
            except Exception as exc:
                summary, compacted = deterministic_summary(
                    messages, keep_recent=self.keep_recent_messages
                )
                self._append(
                    f"compact-degraded:{turn}", "error",
                    {**detail, "status": "compressor_failed",
                     "error": f"{type(exc).__name__}: {exc}"[:2000]},
                )
        payload = {"summary": summary, "messages": compacted}
        self._append(f"compact:{turn}", "context_compacted", payload)
        record = getattr(self.writer, "record_compaction", None)
        if callable(record):
            record(summary, token_estimate=detail.get("tokens"))
        self.writer.save_checkpoint(
            {"turn": turn, "status": "compacted", "message_count": len(compacted),
             **detail}
        )
        # Use the same projection the restore path builds, so a compaction
        # mid-run and one replayed after a restart look identical.
        return compacted_history(payload)

    def _pending_tool_calls(self) -> list[ToolCall]:
        """Find calls whose side effect outcome was not durably recorded."""
        calls: dict[str, ToolCall] = {}
        completed: set[str] = set()
        for event in self._all_events():
            payload = event.get("payload") or {}
            call_id = payload.get("id")
            if event["kind"] == "tool_call" and call_id:
                candidate = ToolCall(
                    str(call_id), str(payload.get("name") or ""),
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                )
                previous = calls.get(candidate.id)
                if previous is not None and previous != candidate:
                    raise RuntimeError("tool call id was reused with different arguments")
                calls[candidate.id] = candidate
            elif event["kind"] == "tool_result" and call_id:
                completed.add(str(call_id))
        return [call for call_id, call in calls.items() if call_id not in completed]

    def _completed_call_ids(self) -> set[str]:
        completed: set[str] = set()
        for event in self._all_events():
            if event["kind"] == "tool_result":
                payload = event.get("payload") or {}
                if payload.get("id"):
                    completed.add(str(payload["id"]))
        return completed

    @staticmethod
    def _dangling_provider_calls(
        messages: list[dict[str, Any]], completed: set[str], known: list[ToolCall]
    ) -> list[ToolCall]:
        """Find tool calls in provider history that never got a result.

        A batch containing a terminal action cancels the loop after the first
        call, so the remaining calls of that batch are persisted inside the
        assistant message without a matching ``tool_call`` event.  A provider
        rejects that transcript on the next turn; repair it with an explicit
        unknown result so the session stays replayable.
        """
        seen = {call.id for call in known}
        found: list[ToolCall] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for raw in message.get("tool_calls") or []:
                if not isinstance(raw, dict):
                    continue
                call_id = str(raw.get("id") or "")
                if not call_id or call_id in completed or call_id in seen:
                    continue
                function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
                found.append(ToolCall(call_id, str(function.get("name") or ""), {}))
                seen.add(call_id)
        return found

    def _restore_unknown_side_effects(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Fence calls interrupted by a crash or cancellation before the result."""
        if self.adapter is None:
            return messages
        pending = self._pending_tool_calls()
        dangling = self._dangling_provider_calls(
            messages, self._completed_call_ids(), pending
        )
        repaired = [*pending, *dangling]
        for call in repaired:
            unknown = {
                "status": "unknown",
                "reason": "worker restarted or cancelled before the side-effect result was persisted",
            }
            result_message = self.adapter.tool_result(call, unknown)
            self._append(
                f"tool-result:{call.id}", "tool_result",
                {"id": call.id, "message": result_message},
            )
            messages.append(result_message)
        if repaired:
            self.writer.save_checkpoint(
                {"status": "unknown_side_effect", "calls": [call.id for call in repaired]}
            )
        return messages

    @staticmethod
    def _batch_signature(calls: list[ToolCall]) -> str:
        body = [{"name": call.name, "arguments": call.arguments} for call in calls]
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _record_truncated_turn(
        self, exc: TurnTruncated, turn_number: int, attempt: int, deltas: list[str]
    ) -> list[dict[str, Any]]:
        """Persist a partial turn and the directive that continues it.

        Running out of output budget says nothing about the session being
        unrecoverable.  Both the partial assistant message and the continuation
        instruction become durable events so a restart replays exactly the
        transcript the provider already saw.
        """
        if attempt >= self.truncation_retry_limit:
            self._append(
                f"truncation-exhausted:{turn_number}", "compression_failed",
                {"reason": exc.reason, "attempts": attempt,
                 "limit": self.truncation_retry_limit},
            )
            self.writer.save_checkpoint(
                {"turn": turn_number, "status": "truncation_exhausted",
                 "reason": exc.reason}
            )
            raise RuntimeError(
                "provider kept truncating its turn at the output limit"
            ) from exc
        continuation = {"role": "user", "content": CONTINUATION_INSTRUCTION}
        self._append(
            f"provider:{turn_number}:truncated:{attempt}", "provider_messages",
            {"messages": [*exc.turn.messages, continuation],
             "text": "".join(deltas), "truncated": exc.reason},
        )
        self.writer.save_checkpoint(
            {"turn": turn_number, "status": "truncated", "reason": exc.reason,
             "attempt": attempt}
        )
        return [*exc.turn.messages, continuation]

    def _bootstrap(self, initial_message: str | None) -> list[dict[str, Any]]:
        if self.adapter is None or self.executor is None:
            raise RuntimeError("this runtime is configured for event recording only")
        messages = self._history()
        messages = self._restore_unknown_side_effects(messages)
        if not messages:
            if not initial_message or not initial_message.strip():
                raise ValueError("a new session requires an initial message")
            first = {"role": "user", "content": initial_message.strip()}
            self._append("session:user:initial", "provider_messages", {"messages": [first]})
            messages.append(first)
        return messages

    def _record_usage(self, turn_number: int, status: str, usage: dict | None) -> None:
        # Each real prompt_tokens report tightens the local estimate, so the
        # compaction threshold tracks the provider's tokenizer over time.
        if usage and usage.get("prompt_tokens") and self._last_estimate:
            updated = calibrate(
                self.calibration, self._last_estimate, usage.get("prompt_tokens")
            )
            if updated != self.calibration:
                self.calibration = updated
                save = getattr(self.writer, "save_calibration", None)
                if callable(save):
                    save(updated)
        record = getattr(self.writer, "record_usage", None)
        if callable(record):
            record(turn_number, status, usage)

    def _budget_exhausted(self, turn_number: int) -> bool:
        return self.max_turns > 0 and turn_number > self.max_turns

    def run(self, initial_message: str | None = None) -> SessionResult:
        messages = self._bootstrap(initial_message)
        turn_number = self._completed_turns
        last_signature = ""
        repeat_count = 0
        last_text = ""
        truncated_attempts = 0
        while not self.cancel.is_set():
            turn_number += 1
            if self._budget_exhausted(turn_number):
                return SessionResult("turn_budget", turn_number - 1, last_text)
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = self._compact(messages, turn_number)
            deltas: list[str] = []

            def on_delta(text: str) -> None:
                deltas.append(text)
                self.emit(text)

            try:
                turn = self.adapter.turn(
                    messages, self.system, self.tools, on_delta, self.cancel
                )
            except TurnTruncated as exc:
                truncated_attempts += 1
                self._record_usage(turn_number, "truncated", getattr(exc.turn, "usage", None))
                messages.extend(
                    self._record_truncated_turn(
                        exc, turn_number, truncated_attempts, deltas
                    )
                )
                last_text = exc.turn.text or last_text
                continue
            truncated_attempts = 0
            last_text = turn.text
            self._append(
                f"provider:{turn_number}", "provider_messages",
                {"messages": turn.messages, "text": "".join(deltas)},
            )
            self._record_usage(turn_number, "completed", getattr(turn, "usage", None))
            messages.extend(turn.messages)
            if not turn.tools:
                self.writer.save_checkpoint(
                    {"turn": turn_number, "status": "idle", "message_count": len(messages)}
                )
                return SessionResult("idle", turn_number, last_text)

            signature = self._batch_signature(turn.tools)
            repeat_count = repeat_count + 1 if signature == last_signature else 1
            last_signature = signature
            if repeat_count >= self.repeated_batch_limit:
                self._append(
                    f"loop:{turn_number}", "loop_detected",
                    {"signature": signature, "repetitions": repeat_count},
                )
                raise RuntimeError("provider repeated an identical tool batch without progress")

            for call in turn.tools:
                if self.cancel.is_set():
                    return SessionResult("cancelled", turn_number, last_text)
                self._append(
                    f"tool-call:{call.id}", "tool_call",
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                )
                output = self.executor.execute(
                    call.name, call.arguments,
                    idempotency_key=f"{self.session_id}:{call.id}",
                )
                result_message = self.adapter.tool_result(call, output)
                self._append(
                    f"tool-result:{call.id}", "tool_result",
                    {"id": call.id, "message": result_message},
                )
                messages.append(result_message)
        return SessionResult("cancelled", turn_number, last_text)

    async def run_async(self, initial_message: str | None = None) -> SessionResult:
        """Run the same durable loop from an async host runtime.

        Provider adapters expose a synchronous streaming method for
        compatibility with the standalone engine.  Running that one blocking
        turn in a worker thread keeps the caller's event loop free for
        cancellation, lease heartbeats and MCP work.  Executors may expose
        ``execute_async``; otherwise their synchronous ``execute`` is isolated
        the same way.
        """
        messages = self._bootstrap(initial_message)
        turn_number = self._completed_turns
        last_signature = ""
        repeat_count = 0
        last_text = ""
        truncated_attempts = 0
        while not self.cancel.is_set():
            turn_number += 1
            if self._budget_exhausted(turn_number):
                return SessionResult("turn_budget", turn_number - 1, last_text)
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = self._compact(messages, turn_number)
            deltas: list[str] = []

            def on_delta(text: str) -> None:
                deltas.append(text)
                self.emit(text)

            try:
                turn = await asyncio.to_thread(
                    self.adapter.turn, messages, self.system, self.tools,
                    on_delta, self.cancel,
                )
            except TurnTruncated as exc:
                truncated_attempts += 1
                self._record_usage(turn_number, "truncated", getattr(exc.turn, "usage", None))
                messages.extend(
                    self._record_truncated_turn(
                        exc, turn_number, truncated_attempts, deltas
                    )
                )
                last_text = exc.turn.text or last_text
                continue
            truncated_attempts = 0
            last_text = turn.text
            self._append(
                f"provider:{turn_number}", "provider_messages",
                {"messages": turn.messages, "text": "".join(deltas)},
            )
            self._record_usage(turn_number, "completed", getattr(turn, "usage", None))
            messages.extend(turn.messages)
            if not turn.tools:
                self.writer.save_checkpoint(
                    {"turn": turn_number, "status": "idle", "message_count": len(messages)}
                )
                return SessionResult("idle", turn_number, last_text)

            signature = self._batch_signature(turn.tools)
            repeat_count = repeat_count + 1 if signature == last_signature else 1
            last_signature = signature
            if repeat_count >= self.repeated_batch_limit:
                self._append(
                    f"loop:{turn_number}", "loop_detected",
                    {"signature": signature, "repetitions": repeat_count},
                )
                raise RuntimeError("provider repeated an identical tool batch without progress")

            for call in turn.tools:
                if self.cancel.is_set():
                    return SessionResult("cancelled", turn_number, last_text)
                self._append(
                    f"tool-call:{call.id}", "tool_call",
                    {"id": call.id, "name": call.name, "arguments": call.arguments},
                )
                execute_async = getattr(self.executor, "execute_async", None)
                if callable(execute_async):
                    output = execute_async(
                        call.name, call.arguments,
                        idempotency_key=f"{self.session_id}:{call.id}",
                    )
                    if hasattr(output, "__await__"):
                        output = await output
                else:
                    output = await asyncio.to_thread(
                        self.executor.execute, call.name, call.arguments,
                        idempotency_key=f"{self.session_id}:{call.id}",
                    )
                    # A lightweight bridge may expose an ``async def execute``
                    # without using the optional ``execute_async`` name.  Do not
                    # leave that coroutine un-awaited.
                    if hasattr(output, "__await__"):
                        output = await output
                result_message = self.adapter.tool_result(call, output)
                self._append(
                    f"tool-result:{call.id}", "tool_result",
                    {"id": call.id, "message": result_message},
                )
                messages.append(result_message)
        return SessionResult("cancelled", turn_number, last_text)
