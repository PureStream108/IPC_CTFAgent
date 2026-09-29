"""Compatibility shim: the durable loop now lives in :mod:`backend.agent.runtime`.

``SessionRunner`` keeps its assignment-shaped constructor so existing
competition callers are unchanged, while the loop itself is the shared
:class:`~backend.agent.runtime.AgentRuntime`. Removed once the Member bridge
constructs the runtime directly.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from backend.agent.runtime import (
    AgentRuntime,
    AssignmentSessionWriter,
    SessionResult,
    ToolExecutor,
)
from backend.competition.conversation import ConversationAdapter

__all__ = ["SessionResult", "SessionRunner", "ToolExecutor"]


class SessionRunner(AgentRuntime):
    """An :class:`AgentRuntime` fenced by a competition solver assignment."""

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
        before_turn: Callable[["SessionRunner"], list[dict[str, Any]]] | None = None,
        context_bytes: int = 1_000_000,
        repeated_batch_limit: int = 8,
        truncation_retry_limit: int = 3,
        max_turns: int = 0,
        context_token_limit: int = 0,
        compaction_trigger_ratio: float = 0.75,
        calibration: float = 1.0,
        keep_recent_messages: int = 8,
    ) -> None:
        super().__init__(
            AssignmentSessionWriter(store, assignment),
            adapter,
            executor,
            system=system,
            tools=tools,
            cancel=cancel,
            emit=emit,
            compressor=compressor,
            before_turn=before_turn,
            context_bytes=context_bytes,
            repeated_batch_limit=repeated_batch_limit,
            truncation_retry_limit=truncation_retry_limit,
            max_turns=max_turns,
            context_token_limit=context_token_limit,
            compaction_trigger_ratio=compaction_trigger_ratio,
            calibration=calibration,
            keep_recent_messages=keep_recent_messages,
        )

    @property
    def store(self):
        return self.writer.store

    @property
    def assignment(self) -> dict:
        return self.writer.assignment
