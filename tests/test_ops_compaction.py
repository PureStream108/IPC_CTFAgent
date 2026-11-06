from __future__ import annotations

import json

from backend.agent.runtime import AgentRuntime
from backend.agent.session import SessionConflict


class MemoryWriter:
    """Append-only transcript with the same idempotency rule as the store."""

    def __init__(self, session_id: str = "ops_s1") -> None:
        self._session_id = session_id
        self.epoch = 1
        self.items: list[dict] = []
        self.checkpoints: list[dict] = []
        self.usage: list[tuple] = []
        self.calibrations: list[float] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    def append(self, event_key, kind, payload):
        existing = next((item for item in self.items if item["event_key"] == event_key), None)
        if existing:
            if existing["kind"] != kind or existing["payload"] != payload:
                raise SessionConflict("event key reused with different content")
            return existing
        item = {
            "session_id": self._session_id,
            "sequence": len(self.items) + 1,
            "event_key": event_key,
            "kind": kind,
            "payload": payload,
        }
        self.items.append(item)
        return item

    def events(self, after=0, limit=500):
        return [item for item in self.items if item["sequence"] > after][:limit]

    def save_checkpoint(self, checkpoint):
        self.checkpoints.append(checkpoint)

    def record_usage(self, turn, status, usage=None):
        self.usage.append((turn, status, usage))

    def save_calibration(self, calibration):
        self.calibrations.append(calibration)

    def record_compaction(self, summary, *, token_estimate=None):
        self.checkpoints.append({"compaction": summary, "tokens": token_estimate})


class Executor:
    def execute(self, name, arguments, *, idempotency_key):
        raise AssertionError("manual compaction must not run tools")


def _seed(writer: MemoryWriter, turns: int = 20) -> None:
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "set up the i春秋 workflow"}]},
    )
    for index in range(turns):
        writer.append(
            f"provider:{index + 1}", "provider_messages",
            {"messages": [{"role": "assistant", "content": f"step {index} " + "x" * 400}]},
        )


def _runtime(writer: MemoryWriter, compressor=None) -> AgentRuntime:
    return AgentRuntime(
        writer,
        None,
        Executor(),
        system="ipc",
        tools=[],
        compressor=compressor or (
            lambda _messages: (
                {"verified_facts": [{"claim": "workflow wf_1 is confirmed", "evidence": "chat"}]},
                [{"role": "user", "content": "recent tail"}],
            )
        ),
    )


def test_manual_compact_rewrites_the_projection_without_a_budget_trigger():
    writer = MemoryWriter()
    _seed(writer)
    runtime = _runtime(writer)

    result = runtime.compact()

    assert result["compacted"] is True
    assert result["messages_before"] == 21
    assert result["messages_after"] < result["messages_before"]
    assert result["degraded"] is False
    kinds = [item["kind"] for item in writer.items]
    assert "context_compacted" in kinds
    assert any(item.get("compaction") for item in writer.checkpoints)

    # the next turn projects from the summary, not the raw transcript
    projected = runtime._history()
    assert projected[0]["content"].startswith("[compacted context summary]")
    assert "workflow wf_1 is confirmed" in projected[0]["content"]
    assert not any("step 5 " in str(message.get("content", "")) for message in projected)


def test_two_manual_compactions_do_not_collide_on_the_event_key():
    writer = MemoryWriter()
    _seed(writer)
    runtime = _runtime(writer)

    first = runtime.compact()
    second = runtime.compact()

    assert first["compacted"] is True
    assert second["compacted"] is False  # already short: nothing left to drop
    assert second["summary"] == {}
    keys = [item["event_key"] for item in writer.items if item["kind"] == "context_compacted"]
    assert keys == ["compact:manual:1:1"]


def test_manual_compact_on_a_single_message_does_nothing():
    writer = MemoryWriter()
    writer.append(
        "session:user:initial", "provider_messages",
        {"messages": [{"role": "user", "content": "hello"}]},
    )

    result = _runtime(writer).compact()

    assert result["compacted"] is False
    assert not any(item["kind"] == "context_compacted" for item in writer.items)


def test_manual_compact_keeps_degraded_summaries_visible():
    writer = MemoryWriter()
    _seed(writer)

    def broken(_messages):
        raise RuntimeError("summarizer endpoint is down")

    result = _runtime(writer, compressor=broken).compact()

    assert result["compacted"] is True
    assert result["degraded"] is True
    errors = [item for item in writer.items if item["kind"] == "error"]
    assert errors and errors[0]["payload"]["status"] == "compressor_failed"


def test_long_transcripts_are_summarized_in_merged_chunks(monkeypatch):
    from backend.ops import agent_bridge

    calls: list[str] = []

    class FakeAdapter:
        def chat(self, messages, *, system_prompt, temperature=0.0):
            calls.append(messages[0]["content"])
            index = len(calls)
            return json.dumps(
                {
                    "verified_facts": [
                        {"claim": f"fact {index}", "evidence": "chat"},
                        {"claim": "shared fact", "evidence": "chat"},
                    ]
                }
            )

    monkeypatch.setattr(
        "backend.members.adapters.make_adapter", lambda *_a, **_k: FakeAdapter()
    )
    monkeypatch.setattr(agent_bridge, "COMPACTION_CHUNK_CHARS", 400)

    history = [{"role": "user", "content": "start"}] + [
        {"role": "assistant", "content": f"step {index} " + "z" * 300}
        for index in range(30)
    ]
    compress = agent_bridge.build_ops_compressor(object())
    summary, _tail = compress(history)

    assert len(calls) > 1
    claims = [fact["claim"] for fact in summary["verified_facts"]]
    assert "shared fact" in claims
    assert claims.count("shared fact") == 1  # deduplicated across chunks
    assert len(claims) >= len(calls)
    assert "chunks" in compress.last_metadata


def test_merged_summaries_stay_structured_and_within_budget(monkeypatch):
    from backend.ops import agent_bridge

    class VerboseAdapter:
        def chat(self, messages, *, system_prompt, temperature=0.0):
            return json.dumps(
                {
                    "verified_facts": [
                        {"claim": f"claim {index} " + "w" * 600, "evidence": "chat"}
                        for index in range(10)
                    ],
                    "failed_paths": [{"approach": f"dead end {index}"} for index in range(10)],
                }
            )

    monkeypatch.setattr(
        "backend.members.adapters.make_adapter", lambda *_a, **_k: VerboseAdapter()
    )
    monkeypatch.setattr(agent_bridge, "COMPACTION_CHUNK_CHARS", 300)

    history = [{"role": "user", "content": "start"}] + [
        {"role": "assistant", "content": f"step {index} " + "z" * 200} for index in range(20)
    ]
    compress = agent_bridge.build_ops_compressor(object())
    summary, _tail = compress(history)

    payload = json.dumps(summary, ensure_ascii=False)
    assert len(payload.encode("utf-8")) <= agent_bridge.COMPACTION_SUMMARY_BYTES
    assert isinstance(summary["verified_facts"], list)
    assert isinstance(summary["verified_facts"][0], dict)
    assert all(len(fact["claim"]) <= 320 for fact in summary["verified_facts"])
    assert "text" not in summary


def test_compact_instructions_reach_the_summarizer(monkeypatch):
    from backend.ops.agent_bridge import build_ops_compressor

    captured: dict[str, str] = {}

    class FakeAdapter:
        def chat(self, messages, *, system_prompt, temperature=0.0):
            captured["system"] = system_prompt
            return json.dumps({"verified_facts": [{"claim": "kept", "evidence": "chat"}]})

    monkeypatch.setattr(
        "backend.members.adapters.make_adapter", lambda *_a, **_k: FakeAdapter()
    )
    history = [{"role": "user", "content": "start"}] + [
        {"role": "assistant", "content": f"step {index} " + "y" * 200}
        for index in range(12)
    ]
    compress = build_ops_compressor(object(), instructions="keep the workflow id")
    summary, _tail = compress(list(history))

    assert summary["verified_facts"][0]["claim"] == "kept"
    assert "keep the workflow id" in captured["system"]

    build_ops_compressor(object())(list(history))
    assert "Operator instructions" not in captured["system"]
