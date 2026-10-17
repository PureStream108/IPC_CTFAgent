from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from backend.agent.compaction import deterministic_summary, fit_history
from backend.agent.context import (
    calibrate,
    compacted_history,
    estimate_tokens,
    infer_context_limit,
    should_compact,
)
from backend.agent.session import SessionConflict, SessionNeedsRebuild
from backend.competition.conversation import (
    CONTINUATION_INSTRUCTION,
    ToolCall,
    TurnTruncated,
)
from backend.competition.store import CompetitionConflict

_recovery_logger = logging.getLogger(__name__)

_EVENT_KEY_REUSE_MESSAGE = "event key reused with different content"


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

    def renew(self) -> bool:
        """Extend this worker's lease so a long turn is not mistaken for death."""
        return bool(
            self.store.renew_writer(self._session_id, self.owner, self.epoch)
        )

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

    def record_usage(self, turn: int, status: str, usage: dict | None = None) -> None:
        """Mirror Ops' turn accounting onto the competition seat.

        Writing a turn row is also the natural place for the worker to prove it
        is alive: it happens once per provider response, which is exactly the
        cadence the watchdog needs to tell "slow turn" from "stopped".
        """
        self.store.record_turn(self.assignment, turn, status, usage=usage)
        self.renew()

    def renew(self) -> bool:
        """Refresh this worker's own liveness lease, if the store supports it."""
        renew = getattr(self.store, "renew_assignment_writer", None)
        if not callable(renew):
            return False
        return bool(renew(self.assignment))

    def save_calibration(self, calibration: float) -> None:
        session = self.store.session(self.session_id)
        budget = dict(session.get("token_budget") or {})
        budget["calibration_ratio"] = calibration
        self.store.save_token_budget(self.session_id, budget)

    def record_provider_progress(self, turn: int, status: str, seconds: float) -> None:
        if not getattr(self.store, "provider_progress_events", False):
            return
        if status == "provider.started":
            self.store.record_turn(self.assignment, turn, "running")
        self.append(
            f"provider-progress:{turn}:{time.monotonic_ns()}", "status",
            {"status": status, "turn": turn, "latency_seconds": round(seconds, 6)},
        )


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
        emit_reasoning: Callable[[str], None] | None = None,
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
        model_snapshot: dict[str, Any] | str | None = None,
        parallel_safe: Callable[[str], bool] | None = None,
        is_terminal: Callable[[str], bool] | None = None,
        max_parallel_tools: int = 4,
    ) -> None:
        self.writer = writer
        self.adapter = adapter
        self.executor = executor
        self.system = system
        self.tools = tools
        self.cancel = cancel or threading.Event()
        self.emit = emit or (lambda _text: None)
        self.emit_reasoning = emit_reasoning or (lambda _text: None)
        self.compressor = compressor
        self.before_turn = before_turn
        self.context_bytes = max(16_384, context_bytes)
        self.repeated_batch_limit = max(3, repeated_batch_limit)
        self.truncation_retry_limit = max(1, truncation_retry_limit)
        self.max_turns = max(0, max_turns)
        self.context_token_limit = max(0, context_token_limit) or infer_context_limit(
            model_snapshot if model_snapshot is not None
            else getattr(adapter, "config", None),
            max_output_tokens=getattr(adapter, "max_output_tokens", 0) or 0,
        )
        self.compaction_trigger_ratio = compaction_trigger_ratio
        self.calibration = calibration
        self.keep_recent_messages = max(2, keep_recent_messages)
        self.parallel_safe = parallel_safe or (lambda _name: False)
        self.is_terminal = is_terminal or (lambda _name: False)
        self.max_parallel_tools = max(1, max_parallel_tools)
        self._completed_turns = 0
        self._last_estimate = 0

    @property
    def session_id(self) -> str:
        return self.writer.session_id

    def _append(self, event_key: str, kind: str, payload: dict[str, Any]) -> dict:
        try:
            return self.writer.append(event_key, kind, payload)
        except (SessionConflict, CompetitionConflict) as exc:
            if _EVENT_KEY_REUSE_MESSAGE in str(exc):
                raise SessionNeedsRebuild(
                    f"event key {event_key!r} collided with durably different content",
                    reason="event_key_conflict",
                ) from exc
            raise

    def _writer_epoch(self) -> int:
        """The current owner epoch, used to scope continuation keys.

        The epoch advances on every handoff/rebuild, so a key that embeds it
        can never collide across generations: a rebuilt session writing a
        differently worded continuation lands under a fresh key instead of
        hitting the reused-key conflict. Writers that do not model an epoch
        (the in-memory test writer, bootstrap-only recorders) report 0, which
        is a stable scope for a single-generation session.
        """
        epoch = getattr(self.writer, "epoch", None)
        if epoch is None:
            assignment = getattr(self.writer, "assignment", None)
            if isinstance(assignment, dict):
                epoch = assignment.get("epoch")
        try:
            return int(epoch)
        except (TypeError, ValueError):
            return 0

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
            if event["kind"] in ("provider_messages", "user_message"):
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
                messages = compacted_history(event["payload"])
        return messages

    def record_user_message(
        self, content: str, *, event_key: str = "session:user:initial"
    ) -> dict:
        return self.writer.append(
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

        started = time.monotonic()
        self._append(f"compact-start:{self._writer_epoch()}:{turn}", "status",
                     {"status": "compaction.started", "turn": turn, **detail})

        summary: Any
        compacted: list[dict[str, Any]]
        if self.compressor is None:
            summary, compacted = deterministic_summary(
                messages, keep_recent=self.keep_recent_messages
            )
            self._append(
                f"compact-degraded:{self._writer_epoch()}:{turn}", "error",
                {**detail, "status": "no_compressor",
                 "note": "compacted deterministically; no semantic summarizer configured"},
            )
        else:
            try:
                summary, compacted = self.compressor(messages)
                if not compacted:
                    raise ValueError("context compressor returned no provider messages")
            except Exception as exc:
                _recovery_logger.debug('Error handled in _compact', exc_info=True)
                summary, compacted = deterministic_summary(
                    messages, keep_recent=self.keep_recent_messages
                )
                self._append(
                    f"compact-degraded:{self._writer_epoch()}:{turn}", "error",
                    {**detail, "status": "compressor_failed",
                     "error": f"{type(exc).__name__}: {exc}"[:2000]},
                )
        from backend.sandbox.output import preview_text

        summary_budget = max(1024, min(8192, self.context_bytes // 8))
        if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > summary_budget:
            summary = {"text": preview_text(json.dumps(summary, ensure_ascii=False), summary_budget - 128)}
        compacted = fit_history(
            compacted, max_bytes=max(2048, int(self.context_bytes * .6) - summary_budget),
            max_tokens=max(512, int(self.context_token_limit * .5)) if self.context_token_limit else 0,
            calibration=self.calibration,
        )
        metadata = dict(getattr(self.compressor, "last_metadata", {}))
        fallback = "compacted without a semantic summary" in json.dumps(summary, ensure_ascii=False)
        payload = {"summary": summary, "messages": compacted, "metrics": {
            "duration_seconds": round(time.monotonic() - started, 6),
            "input_tokens": detail.get("tokens"), "output_tokens": estimate_tokens(compacted),
            "output_bytes": len(json.dumps(compacted, ensure_ascii=False).encode("utf-8")),
            "degraded": fallback or bool(metadata.get("degraded_reason")), **metadata,
        }}
        self._append(f"compact:{self._writer_epoch()}:{turn}", "context_compacted", payload)
        self._append(f"compact-end:{self._writer_epoch()}:{turn}", "status",
                     {"status": "compaction.completed", "turn": turn, **payload["metrics"]})
        record = getattr(self.writer, "record_compaction", None)
        if callable(record):
            record(summary, token_estimate=detail.get("tokens"))
        self.writer.save_checkpoint(
            {"turn": turn, "status": "compacted", "message_count": len(compacted),
             **detail}
        )
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
    def _normalize_tool_order(
        messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Reattach tool results to the assistant turn that declared them.

        A crash between an assistant ``tool_calls`` batch and its results lets a
        later user message (the restart directive) and a synthesized ``tool``
        result land out of order in the durable transcript.  Strict providers
        reject that projection with an opaque 400 on every replay, so the
        bootstrap moves each orphaned tool message directly behind its
        declaring assistant message instead of trusting event order.
        """
        skip: set[int] = set()
        result: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if index in skip:
                continue
            result.append(message)
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            declared = {
                str(raw.get("id"))
                for raw in message.get("tool_calls") or []
                if isinstance(raw, dict) and raw.get("id")
            }
            if not declared:
                continue
            for later in range(index + 1, len(messages)):
                if later in skip:
                    continue
                candidate = messages[later]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    continue
                if str(candidate.get("tool_call_id") or "") in declared:
                    skip.add(later)
                    result.append(candidate)
        return result

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
        """Rebuild the transcript and append the caller's new message to it.

        A session that already has history is the normal case for a
        conversation: the operator sends a second message to a session that
        already answered a first one. That message has to become a durable
        event like any other, or the loop re-runs over the old history and
        answers the previous question again.

        The continuation key is stable per (epoch, content) rather than derived
        from the transcript length. A position key drifts: the same logical
        message lands under a different key once the event count changes (a
        repaired turn, a compaction), so a replay re-appends it as a duplicate
        or, worse, a genuinely new message reuses an old position and trips the
        reused-key conflict. Embedding the epoch keeps a rebuilt generation from
        colliding with the one it replaced; embedding the content makes a replay
        of the identical message idempotent while two different messages keep
        distinct keys.
        """
        if self.adapter is None or self.executor is None:
            raise RuntimeError("this runtime is configured for event recording only")
        messages = self._history()
        messages = self._restore_unknown_side_effects(messages)
        messages = self._normalize_tool_order(messages)
        if not messages:
            if not initial_message or not initial_message.strip():
                raise ValueError("a new session requires an initial message")
            first = {"role": "user", "content": initial_message.strip()}
            self._append("session:user:initial", "provider_messages", {"messages": [first]})
            return [first]
        if initial_message and initial_message.strip():
            text = initial_message.strip()
            if not self._is_last_user_message(messages, text):
                follow_up = {"role": "user", "content": text}
                self._append(
                    self._continuation_key(text),
                    "user_message", {"messages": [follow_up]},
                )
                messages.append(follow_up)
        return messages

    def _continuation_key(self, text: str) -> str:
        """Stable per-(epoch, content) key for a resumed-session user message.

        Content lives in the key via a short digest so the identical message
        always maps to the same event (idempotent replay) and two different
        messages never share a key (no false reused-key conflict). The epoch
        prefix scopes the key to the current owner generation: a rebuild writing
        a reworded continuation lands under a fresh namespace instead of
        colliding with the stalled generation's events.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return f"session:user:continue:{self._writer_epoch()}:{digest}"

    @staticmethod
    def _is_last_user_message(messages: list[dict[str, Any]], text: str) -> bool:
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            return message.get("content") == text
        return False

    def _record_usage(self, turn_number: int, status: str, usage: dict | None) -> None:
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

    def _tool_groups(self, calls: list[ToolCall]) -> list[list[ToolCall]]:
        """Split one turn's tool calls into sequentially executed groups.

        Consecutive read-only calls become one group that may run
        concurrently; anything with a side effect is its own group, so its
        relative order with its neighbours is exactly what the provider asked
        for.

        Grouping only *adjacent* safe calls is the point: hoisting a read
        across a write would let the model observe state from before a change
        it had already requested.

        Group size is capped because the fan-out is provider-controlled. A
        model can emit dozens of reads in one batch, and each one takes a
        database connection and possibly a sandbox exec; an unbounded
        ``gather`` would exhaust the connection pool and stall the whole
        session rather than making it faster.
        """
        groups: list[list[ToolCall]] = []
        for call in calls:
            concurrent = self.parallel_safe(call.name) and not self.is_terminal(call.name)
            previous = groups[-1][-1] if groups else None
            extendable = (
                concurrent
                and previous is not None
                and len(groups[-1]) < self.max_parallel_tools
                and self.parallel_safe(previous.name)
                and not self.is_terminal(previous.name)
            )
            if extendable:
                groups[-1].append(call)
            else:
                groups.append([call])
        return groups

    def _turn_args(self, messages: list[dict[str, Any]], on_delta, on_reasoning):
        """Positional arguments for ``adapter.turn``.

        ``emit_reasoning`` is appended only for adapters that advertise it, so
        the five-argument doubles used throughout the tests keep working.
        """
        args = [messages, self.system, self.tools, on_delta, self.cancel]
        if getattr(self.adapter, "emits_reasoning", False):
            args.append(on_reasoning)
        return args

    def _provider_callbacks(self, turn: int):
        sequences = getattr(self, "recon_sequences", [])
        if sequences:
            self._append(f"recon-context:{self._writer_epoch()}:{turn}", "status",
                         {"status": "recon.in_provider_context", "turn": turn, "sequences": sequences})
            self.recon_sequences = []
        recorder = getattr(self.writer, "record_provider_progress", None)
        if callable(recorder):
            recorder(turn, "provider.started", 0.0)
        started = time.monotonic()
        responded = False
        content_received = False

        def first_response():
            nonlocal responded
            if not responded:
                responded = True
                if callable(recorder):
                    recorder(turn, "provider.first_response", time.monotonic() - started)

        def completed():
            first_response()
            if callable(recorder):
                recorder(turn, "provider.completed", time.monotonic() - started)

        def first_content():
            nonlocal content_received
            if not content_received:
                content_received = True
                if callable(recorder):
                    recorder(turn, "provider.first_content", time.monotonic() - started)

        if hasattr(self.adapter, "on_first_response"):
            self.adapter.on_first_response = first_response
        if hasattr(self.adapter, "on_first_content"):
            self.adapter.on_first_content = first_content
        if hasattr(self.adapter, "on_progress") and callable(recorder):
            self.adapter.on_progress = lambda: recorder(turn, "provider.progress", time.monotonic() - started)
        return first_response, completed

    def _record_tool_call(self, call: ToolCall) -> None:
        self._append(
            f"tool-call:{call.id}", "tool_call",
            {"id": call.id, "name": call.name, "arguments": call.arguments},
        )

    def _record_tool_result(self, call: ToolCall, output: Any) -> dict[str, Any]:
        result_message = self.adapter.tool_result(call, output)
        self._append(
            f"tool-result:{call.id}", "tool_result",
            {"id": call.id, "message": result_message},
        )
        return result_message

    @staticmethod
    def _is_invalid_tool_order_turn(exc: BaseException) -> bool:
        """Classify a provider rejection as an invalid tool-order 400.

        A poisoned transcript - a ``tool`` result with no preceding assistant
        ``tool_calls`` that declares it, or a dangling call with no result -
        makes a strict provider reject every turn with an opaque 400. Only a 400
        qualifies: a 429/5xx is transient and must keep its existing retry path,
        and a non-HTTP error is unrelated to history shape. The runtime stays
        provider-agnostic by duck-typing the status code rather than importing a
        concrete adapter's exception type.
        """
        status = getattr(exc, "status_code", None)
        if status is None:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
        return status == 400

    def _repair_poisoned_history(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """Re-apply the bootstrap repairs to a mid-loop transcript.

        Returns the repaired projection when it actually differs from what was
        just rejected, or ``None`` when repair is a no-op - meaning the 400 is
        not a tool-order problem this pass can fix and the caller must escalate
        rather than retry the identical history forever.

        The repair is the same one the bootstrap trusts: fence any interrupted
        call as ``unknown`` and reattach every orphaned ``tool`` result behind
        the assistant message that declared it. Running it mid-loop lets a
        session that was poisoned *after* it started (a crash between a batch and
        its results) heal in place instead of 400-ing until a watchdog rebuild.
        """
        repaired = self._restore_unknown_side_effects(list(messages))
        repaired = self._normalize_tool_order(repaired)
        if repaired == messages:
            return None
        return repaired

    def _handle_turn_failure(
        self,
        exc: Exception,
        messages: list[dict[str, Any]],
        turn_number: int,
        repaired_turn: int,
    ) -> list[dict[str, Any]]:
        """Self-heal an invalid tool-order 400, or let the error propagate.

        A 400 that a tool-order repair has not yet been tried against gets one
        in-place repair: the next turn runs over the fixed projection and the
        session continues. If repair cannot change the history (``None``), or
        the previous turn already repaired and the provider still rejects it,
        the transcript is poisoned beyond in-loop recovery - escalate to the
        shared new-epoch rebuild (``session.rebuilt`` reason=invalid_tool_order)
        rather than looping on the same poison.

        Any other failure (truncation handled earlier, transient 429/5xx, a
        provider auth error) is not ours to swallow: re-raise so the existing
        provider-error classification and retry path upstream still apply.
        """
        if not self._is_invalid_tool_order_turn(exc):
            raise exc
        if repaired_turn == turn_number:
            raise SessionNeedsRebuild(
                "transcript still rejected after an in-loop tool-order repair",
                reason="invalid_tool_order",
            ) from exc
        repaired = self._repair_poisoned_history(messages)
        if repaired is None:
            raise SessionNeedsRebuild(
                "provider rejected the transcript and no tool-order repair applies",
                reason="invalid_tool_order",
            ) from exc
        self._append(
            f"session-heal:{turn_number}", "status",
            {"status": "tool_order_repaired", "reason": "invalid_tool_order"},
        )
        self.writer.save_checkpoint(
            {"turn": turn_number, "status": "tool_order_repaired",
             "reason": "invalid_tool_order"}
        )
        return repaired

    def run(self, initial_message: str | None = None) -> SessionResult:
        messages = self._bootstrap(initial_message)
        turn_number = self._completed_turns
        last_signature = ""
        repeat_count = 0
        last_text = ""
        truncated_attempts = 0
        repaired_turn = -1
        while not self.cancel.is_set():
            turn_number += 1
            if self._budget_exhausted(turn_number):
                return SessionResult("turn_budget", turn_number - 1, last_text)
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = self._compact(messages, turn_number)
            deltas: list[str] = []
            first_response, provider_completed = self._provider_callbacks(turn_number)

            def on_delta(text: str, deltas=deltas, first_response=first_response) -> None:
                first_response()
                deltas.append(text)
                self.emit(text)

            def on_reasoning(text: str, first_response=first_response) -> None:
                first_response()
                self.emit_reasoning(text)

            try:
                turn = self.adapter.turn(
                    *self._turn_args(messages, on_delta, on_reasoning)
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
            except Exception as exc:
                _recovery_logger.debug('Error handled in run', exc_info=True)
                messages = self._handle_turn_failure(
                    exc, messages, turn_number, repaired_turn
                )
                repaired_turn = turn_number
                turn_number -= 1
                continue
            repaired_turn = -1
            provider_completed()
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
                self._record_tool_call(call)
                output = self.executor.execute(
                    call.name, call.arguments,
                    idempotency_key=f"{self.session_id}:{call.id}",
                )
                messages.append(self._record_tool_result(call, output))
                if self.is_terminal(call.name):
                    break
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
        repaired_turn = -1
        while not self.cancel.is_set():
            turn_number += 1
            if self._budget_exhausted(turn_number):
                return SessionResult("turn_budget", turn_number - 1, last_text)
            if self.before_turn is not None:
                messages.extend(self.before_turn(self))
            messages = await asyncio.to_thread(self._compact, messages, turn_number)
            deltas: list[str] = []
            first_response, provider_completed = self._provider_callbacks(turn_number)

            def on_delta(text: str, deltas=deltas, first_response=first_response) -> None:
                first_response()
                deltas.append(text)
                self.emit(text)

            def on_reasoning(text: str, first_response=first_response) -> None:
                first_response()
                self.emit_reasoning(text)

            try:
                turn = await asyncio.to_thread(
                    self.adapter.turn,
                    *self._turn_args(messages, on_delta, on_reasoning),
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
            except Exception as exc:
                _recovery_logger.debug('Error handled in run_async', exc_info=True)
                messages = self._handle_turn_failure(
                    exc, messages, turn_number, repaired_turn
                )
                repaired_turn = turn_number
                turn_number -= 1
                continue
            repaired_turn = -1
            provider_completed()
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

            for group in self._tool_groups(turn.tools):
                if self.cancel.is_set():
                    return SessionResult("cancelled", turn_number, last_text)
                messages.extend(await self._run_tool_group(group))
                if any(self.is_terminal(call.name) for call in group):
                    break
        return SessionResult("cancelled", turn_number, last_text)

    async def _run_tool_group(self, group: list[ToolCall]) -> list[dict[str, Any]]:
        """Execute one group, keeping the transcript in the provider's order.

        Ordering is the subtle part. Appending each call/result pair from
        inside its own coroutine would order the event log by *completion*,
        so a fast read would appear before a slow one the provider had listed
        first. History is rebuilt from that log, so the rebuilt ``tool_calls``
        array would no longer line up with its results.

        So: every call is appended first, in order; the tools then run
        concurrently; and the results are appended in the original order once
        all have returned. ``tool-call:{id}`` and ``tool-result:{id}`` are
        keyed on the provider's call id, which is unique within a turn, so the
        pairing survives concurrency.

        A tool that raises must not take its siblings' results down with it.
        The sequential path records every result up to the failure, and the
        repair path turns a *missing* result into ``unknown`` on the next
        turn - so discarding a result that was actually known would throw away
        real evidence. Hence ``return_exceptions=True``: successes are
        persisted, only the genuinely failed call is left dangling, and the
        error then propagates as before.
        """
        if len(group) == 1:
            call = group[0]
            self._record_tool_call(call)
            return [self._record_tool_result(call, await self._invoke(call))]
        for call in group:
            self._record_tool_call(call)
        outputs = await asyncio.gather(
            *(self._invoke(call) for call in group), return_exceptions=True
        )
        messages: list[dict[str, Any]] = []
        failure: BaseException | None = None
        for call, output in zip(group, outputs):
            if isinstance(output, BaseException):
                failure = failure or output
                continue
            messages.append(self._record_tool_result(call, output))
        if failure is not None:
            raise failure
        return messages

    async def _invoke(self, call: ToolCall) -> Any:
        """Run one tool call, without touching the transcript."""
        execute_async = getattr(self.executor, "execute_async", None)
        if callable(execute_async):
            output = execute_async(
                call.name, call.arguments,
                idempotency_key=f"{self.session_id}:{call.id}",
            )
            if hasattr(output, "__await__"):
                output = await output
            return output
        output = await asyncio.to_thread(
            self.executor.execute, call.name, call.arguments,
            idempotency_key=f"{self.session_id}:{call.id}",
        )
        if hasattr(output, "__await__"):
            output = await output
        return output
