from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.competition.conversation import ConversationAdapter, ToolCall


class ToolExecutor(Protocol):
    def execute(
        self, name: str, arguments: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class SessionResult:
    status: str
    turns: int
    text: str = ""


class SessionRunner:
    """Run a persistent provider-native tool loop without a step budget.

    The loop ends only when the provider completes without requesting a tool,
    cancellation is requested, the assignment lease is lost, or repeated
    identical tool batches indicate an actual loop.  Tool executors receive a
    stable idempotency key and are responsible for returning a prior result
    after a process restart instead of repeating a side effect.
    """

    def __init__(
        self,
        store,
        adapter: ConversationAdapter | None,
        executor: ToolExecutor | None,
        *,
        assignment: dict,
        system: str,
        tools: list[dict[str, Any]],
        cancel: threading.Event | None = None,
        emit: Callable[[str], None] | None = None,
        compressor: Callable[[list[dict[str, Any]]], tuple[str, list[dict[str, Any]]]] | None = None,
        before_turn: Callable[[SessionRunner], list[dict[str, Any]]] | None = None,
        context_bytes: int = 512_000,
        repeated_batch_limit: int = 8,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.executor = executor
        self.assignment = assignment
        self.system = system
        self.tools = tools
        self.cancel = cancel or threading.Event()
        self.emit = emit or (lambda _text: None)
        self.compressor = compressor
        self.before_turn = before_turn
        self.context_bytes = max(16_384, context_bytes)
        self.repeated_batch_limit = max(3, repeated_batch_limit)
        self._completed_turns = 0

    @property
    def session_id(self) -> str:
        return self.assignment["session_id"]

    def _append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        return self.store.append_event(
            self.session_id,
            event_key,
            kind,
            payload,
            assignment_id=self.assignment["id"],
            owner=self.assignment["lease_owner"],
            epoch=self.assignment["epoch"],
        )

    def _history(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        after = 0
        while True:
            events = self.store.events(self.session_id, after=after, limit=500)
            if not events:
                break
            for event in events:
                after = event["sequence"]
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
                    messages = list(event["payload"]["messages"])
            if len(events) < 500:
                break
        return messages

    # The competition Member bridge uses the same runner as the native
    # provider loop, but lets the existing graph/tool dispatcher supply a turn.
    # Keeping these writes here guarantees identical fencing, idempotency and
    # event shapes for both runtimes.
    def record_user_message(self, content: str, *, event_key: str = "session:user:initial") -> dict:
        return self._append(event_key, "provider_messages", {"messages": [{"role": "user", "content": content}]})

    def record_provider_message(
        self, message: dict[str, Any], *, event_key: str, text: str = ""
    ) -> dict:
        return self._append(
            event_key,
            "provider_messages",
            {"messages": [message], "text": text},
        )

    def record_tool_call(self, call: ToolCall, *, event_key: str | None = None) -> dict:
        return self._append(
            event_key or f"tool-call:{call.id}",
            "tool_call",
            {"id": call.id, "name": call.name, "arguments": call.arguments},
        )

    def record_tool_result(
        self, call: ToolCall, message: dict[str, Any], *, event_key: str | None = None
    ) -> dict:
        return self._append(
            event_key or f"tool-result:{call.id}",
            "tool_result",
            {"id": call.id, "message": message},
        )

    def record_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        self.store.save_checkpoint(self.assignment, checkpoint)

    def call_state(self, call_id: str) -> str | None:
        """Return ``pending``/``done`` for a persisted call, if present."""
        after = 0
        state: str | None = None
        while True:
            events = self.store.events(self.session_id, after=after, limit=500)
            if not events:
                break
            for event in events:
                after = event["sequence"]
                payload = event.get("payload") or {}
                if event["kind"] == "tool_call" and payload.get("id") == call_id:
                    state = "pending"
                elif event["kind"] == "tool_result" and payload.get("id") == call_id:
                    state = "done"
            if len(events) < 500:
                break
        return state

    def _compact(self, messages: list[dict[str, Any]], turn: int) -> list[dict[str, Any]]:
        encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) <= self.context_bytes:
            return messages
        if self.compressor is None:
            self._append(
                f"compact-failed:{turn}", "compression_failed",
                {"reason": "context limit exceeded", "bytes": len(encoded),
                 "limit": self.context_bytes},
            )
            self.store.save_checkpoint(
                self.assignment,
                {"turn": turn, "status": "compression_failed", "bytes": len(encoded)},
            )
            raise RuntimeError("provider context exceeds the configured limit")
        try:
            summary, compacted = self.compressor(messages)
        except Exception as exc:
            self._append(
                f"compact-failed:{turn}", "compression_failed",
                {"reason": f"{type(exc).__name__}: {exc}"[:2000],
                 "bytes": len(encoded), "limit": self.context_bytes},
            )
            self.store.save_checkpoint(
                self.assignment,
                {"turn": turn, "status": "compression_failed", "bytes": len(encoded)},
            )
            raise
        if not compacted:
            raise ValueError("context compressor returned no provider messages")
        payload = {"summary": summary, "messages": compacted}
        self._append(f"compact:{turn}", "context_compacted", payload)
        self.store.save_checkpoint(
            self.assignment,
            {"turn": turn, "summary": summary, "message_count": len(compacted)},
        )
        return compacted

    def _pending_tool_calls(self) -> list[ToolCall]:
        """Find calls whose side effect outcome was not durably recorded."""
        calls: dict[str, ToolCall] = {}
        completed: set[str] = set()
        after = 0
        while True:
            events = self.store.events(self.session_id, after=after, limit=500)
            if not events:
                break
            for event in events:
                after = event["sequence"]
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
            if len(events) < 500:
                break
        return [call for call_id, call in calls.items() if call_id not in completed]

    def _restore_unknown_side_effects(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fence calls interrupted by a worker crash before the result event."""
        if self.adapter is None:
            return messages
        pending = self._pending_tool_calls()
        for call in pending:
            unknown = {
                "status": "unknown",
                "reason": "worker restarted before the side-effect result was persisted",
            }
            result_message = self.adapter.tool_result(call, unknown)
            self._append(
                f"tool-result:{call.id}", "tool_result",
                {"id": call.id, "message": result_message},
            )
            messages.append(result_message)
        if pending:
            self.store.save_checkpoint(
                self.assignment,
                {"status": "unknown_side_effect", "calls": [call.id for call in pending]},
            )
        return messages

    @staticmethod
    def _batch_signature(calls: list[ToolCall]) -> str:
        body = [
            {"name": call.name, "arguments": call.arguments}
            for call in calls
        ]
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def run(self, initial_message: str | None = None) -> SessionResult:
        if self.adapter is None or self.executor is None:
            raise RuntimeError("this SessionRunner is configured for event recording only")
        messages = self._history()
        messages = self._restore_unknown_side_effects(messages)
        if not messages:
            if not initial_message or not initial_message.strip():
                raise ValueError("a new session requires an initial message")
            first = {"role": "user", "content": initial_message.strip()}
            self._append("session:user:initial", "provider_messages", {"messages": [first]})
            messages.append(first)

        turn_number = self._completed_turns
        last_signature = ""
        repeat_count = 0
        last_text = ""
        while not self.cancel.is_set():
            turn_number += 1
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = self._compact(messages, turn_number)
            deltas: list[str] = []

            def on_delta(text: str) -> None:
                deltas.append(text)
                self.emit(text)

            turn = self.adapter.turn(
                messages, self.system, self.tools, on_delta, self.cancel
            )
            last_text = turn.text
            self._append(
                f"provider:{turn_number}",
                "provider_messages",
                {"messages": turn.messages, "text": "".join(deltas)},
            )
            messages.extend(turn.messages)
            if not turn.tools:
                self.store.save_checkpoint(
                    self.assignment,
                    {"turn": turn_number, "status": "idle", "message_count": len(messages)},
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
                    call.name,
                    call.arguments,
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
        """Run the same durable loop from an async Member runtime.

        Provider adapters in this repository expose a synchronous streaming
        method for compatibility with the standalone engine.  Running that
        one blocking turn in a worker thread keeps the Member event loop
        available for cancellation, lease heartbeats, and MCP work.  Executors
        may expose ``execute_async``; otherwise their synchronous ``execute``
        method is isolated in the same way.
        """
        if self.adapter is None or self.executor is None:
            raise RuntimeError("this SessionRunner is configured for event recording only")
        messages = self._history()
        messages = self._restore_unknown_side_effects(messages)
        if not messages:
            if not initial_message or not initial_message.strip():
                raise ValueError("a new session requires an initial message")
            first = {"role": "user", "content": initial_message.strip()}
            self._append("session:user:initial", "provider_messages", {"messages": [first]})
            messages.append(first)

        turn_number = self._completed_turns
        last_signature = ""
        repeat_count = 0
        last_text = ""
        while not self.cancel.is_set():
            turn_number += 1
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = self._compact(messages, turn_number)
            deltas: list[str] = []

            def on_delta(text: str) -> None:
                deltas.append(text)
                self.emit(text)

            turn = await asyncio.to_thread(
                self.adapter.turn,
                messages,
                self.system,
                self.tools,
                on_delta,
                self.cancel,
            )
            last_text = turn.text
            self._append(
                f"provider:{turn_number}",
                "provider_messages",
                {"messages": turn.messages, "text": "".join(deltas)},
            )
            messages.extend(turn.messages)
            if not turn.tools:
                self.store.save_checkpoint(
                    self.assignment,
                    {"turn": turn_number, "status": "idle", "message_count": len(messages)},
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
                        call.name,
                        call.arguments,
                        idempotency_key=f"{self.session_id}:{call.id}",
                    )
                    if hasattr(output, "__await__"):
                        output = await output
                else:
                    output = await asyncio.to_thread(
                        self.executor.execute,
                        call.name,
                        call.arguments,
                        idempotency_key=f"{self.session_id}:{call.id}",
                    )
                    # A lightweight bridge may expose an ``async def
                    # execute`` method without using the optional
                    # ``execute_async`` name.  Do not leave that coroutine
                    # un-awaited when it was obtained through the thread
                    # compatibility path.
                    if hasattr(output, "__await__"):
                        output = await output
                result_message = self.adapter.tool_result(call, output)
                self._append(
                    f"tool-result:{call.id}", "tool_result",
                    {"id": call.id, "message": result_message},
                )
                messages.append(result_message)
        return SessionResult("cancelled", turn_number, last_text)
