"""Token budgeting, calibration and semantic compaction.

The core claim under test: a byte threshold is the wrong unit. CJK and ASCII
text differ by roughly a factor of two per token, so a byte-based trigger either
overflows the window or discards evidence that still fit.
"""
from __future__ import annotations

from backend.agent.compaction import (
    deterministic_summary,
    empty_summary,
    is_empty,
    normalize_summary,
    summarize,
)
from backend.agent.context import (
    calibrate,
    compacted_history,
    estimate_text_tokens,
    estimate_tokens,
    should_compact,
    summary_message,
)


def test_cjk_and_ascii_text_are_estimated_at_different_densities():
    """Equal byte counts are not equal token counts."""
    cjk = "这是一段中文测试文本" * 20
    ascii_text = "this is an english sentence" * 20
    assert estimate_text_tokens(cjk) > 0
    assert estimate_text_tokens(ascii_text) > 0
    # Per character, CJK costs materially more tokens than English prose.
    assert (estimate_text_tokens(cjk) / len(cjk)) > (
        estimate_text_tokens(ascii_text) / len(ascii_text)
    )


def test_message_overhead_is_counted_so_many_small_turns_are_not_free():
    one = [{"role": "user", "content": "hi"}]
    many = [{"role": "user", "content": "hi"} for _ in range(50)]
    assert estimate_tokens(many) > estimate_tokens(one) * 40


def test_tool_calls_and_ids_are_included_in_the_estimate():
    plain = [{"role": "assistant", "content": "ok"}]
    with_calls = [
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {"id": "c1", "function": {"name": "task_sandbox_exec",
                                          "arguments": '{"command":"ls -la /workspace"}'}}
            ],
        }
    ]
    assert estimate_tokens(with_calls) > estimate_tokens(plain)


def test_calibration_converges_towards_the_reported_prompt_tokens():
    """Repeated real observations should pull the estimate onto the tokenizer."""
    ratio = 1.0
    # A provider that consistently charges ~1.5x the heuristic.
    for _ in range(25):
        ratio = calibrate(ratio, 1_000, 1_500)
    assert 1.4 <= ratio <= 1.5

    # And back down again when the provider charges less.
    for _ in range(40):
        ratio = calibrate(ratio, 1_000, 800)
    assert 0.8 <= ratio <= 0.85


def test_calibration_ignores_missing_or_nonsense_usage():
    assert calibrate(1.2, 1_000, None) == 1.2
    assert calibrate(1.2, 1_000, 0) == 1.2
    assert calibrate(1.2, 0, 900) == 1.2


def test_calibration_is_clamped_against_a_single_anomalous_response():
    # A cache hit or a differently-counting provider must not wreck the budget.
    assert calibrate(1.0, 10, 10_000_000) <= 2.0
    assert calibrate(1.0, 10_000_000, 1) >= 0.5


def test_compaction_triggers_on_the_budget_ratio_not_at_the_hard_limit():
    messages = [{"role": "user", "content": "x" * 4_000}]
    estimate = estimate_tokens(messages)
    # Just above the trigger point but below the limit: compact now, because
    # waiting for the hard limit means the next turn cannot fit its output.
    limit = int(estimate / 0.8)
    assert should_compact(messages, limit=limit, trigger_ratio=0.75) is True
    assert should_compact(messages, limit=estimate * 10, trigger_ratio=0.75) is False
    # limit=0 means "unknown window": never compact on a guess.
    assert should_compact(messages, limit=0) is False


def test_calibration_shifts_the_compaction_decision():
    messages = [{"role": "user", "content": "x" * 4_000}]
    limit = int(estimate_tokens(messages) / 0.6)
    assert should_compact(messages, limit=limit, trigger_ratio=0.75) is False
    # Once the provider proves it charges more, the same history compacts.
    assert should_compact(
        messages, limit=limit, trigger_ratio=0.75, calibration=1.8
    ) is True


def test_structured_summary_is_normalized_and_junk_is_dropped():
    summary = normalize_summary(
        {
            "verified_facts": [{"claim": "/admin reachable", "evidence": "curl -i"}],
            "failed_paths": {"approach": "sqli", "do_not_retry": True},
            "nonsense": "ignored",
        }
    )
    assert summary["verified_facts"][0]["claim"] == "/admin reachable"
    # A single object is accepted where a list was expected.
    assert summary["failed_paths"] == [{"approach": "sqli", "do_not_retry": True}]
    assert "nonsense" not in summary
    assert set(summary) == set(empty_summary())
    assert is_empty(empty_summary()) is True
    assert is_empty(summary) is False


def test_injected_summary_tells_the_model_not_to_retry_failed_paths():
    summary = {
        "verified_facts": [{"claim": "flag is not in /etc", "evidence": "grep -r"}],
        "failed_paths": [
            {"approach": "sql injection on /login", "why_failed": "prepared statements",
             "do_not_retry": True}
        ],
        "key_offsets": [{"file": "bin/chal", "offset": "0x401f20", "meaning": "win()"}],
    }
    message = summary_message(summary)
    assert message["role"] == "user"
    assert "do not redo the work" in message["content"]
    assert "do_not_retry" in message["content"]
    assert "0x401f20" in message["content"]

    projection = compacted_history(
        {"summary": summary, "messages": [{"role": "user", "content": "tail"}]}
    )
    assert projection[0] == message
    assert projection[1] == {"role": "user", "content": "tail"}


def test_semantic_summarize_uses_the_model_response_when_it_is_usable():
    import json

    messages = [{"role": "user", "content": "start"}]
    messages += [
        {"role": "assistant", "content": f"step {index}"} for index in range(30)
    ]
    expected = {
        "verified_facts": [{"claim": "port 8080 is a Flask app", "evidence": "curl -I"}],
        "failed_paths": [],
        "open_questions": ["is the upload path filtered?"],
        "artifacts": [],
        "key_offsets": [],
        "next_hypotheses": ["try SSTI on the name field"],
    }
    seen: dict = {}

    def request(prompt, dropped):
        seen["prompt"] = prompt
        seen["dropped"] = dropped
        # Models often wrap JSON in prose; that must still parse.
        return "Here you go:\n```json\n" + json.dumps(expected) + "\n```"

    summary, retained = summarize(messages, request=request, keep_recent=4)

    assert summary["verified_facts"] == expected["verified_facts"]
    assert summary["next_hypotheses"] == expected["next_hypotheses"]
    assert "do_not_retry" in seen["prompt"]
    assert len(retained) < len(messages)
    # The opening task statement is never dropped.
    assert retained[0] == {"role": "user", "content": "start"}


def test_semantic_summarize_falls_back_when_the_model_call_fails():
    messages = [{"role": "user", "content": "start"}]
    messages += [
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [{"id": f"c{index}", "function": {"name": "member_action"}}],
        }
        for index in range(30)
    ]

    def failing(prompt, dropped):
        raise RuntimeError("summarizer endpoint is down")

    summary, retained = summarize(messages, request=failing, keep_recent=4)

    # A degraded summary, not an exception: compaction is an optimisation.
    assert "member_action" in summary["verified_facts"][0]["claim"]
    assert retained[0] == {"role": "user", "content": "start"}


def test_semantic_summarize_falls_back_on_an_empty_or_unparseable_reply():
    messages = [{"role": "user", "content": "start"}]
    messages += [{"role": "assistant", "content": "step"} for _ in range(20)]

    for reply in ("", "I could not summarize that.", "{}"):
        summary, retained = summarize(
            messages, request=lambda *_args, **_kwargs: reply, keep_recent=4
        )
        assert summary["open_questions"], reply
        assert retained


def test_deterministic_compaction_never_orphans_a_tool_result():
    """A tail starting with an unanswered tool result is rejected by providers."""
    messages = [{"role": "user", "content": "start"}]
    for index in range(20):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": f"call-{index}", "function": {"name": "bash"}}],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"call-{index}", "content": "output"}
        )
    # An odd keep_recent would otherwise cut between a call and its result.
    _summary, retained = deterministic_summary(messages, keep_recent=5)
    tail = retained[1:]
    assert tail
    assert not (tail[0].get("role") == "tool")


def test_deterministic_compaction_handles_anthropic_tool_result_blocks():
    messages = [{"role": "user", "content": "start"}]
    for index in range(10):
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{index}", "name": "bash",
                             "input": {}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{index}",
                             "content": "out"}],
            }
        )
    _summary, retained = deterministic_summary(messages, keep_recent=3)
    tail = retained[1:]
    # The first retained message must not be a bare tool result block.
    assert not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in (tail[0].get("content") or [])
        if isinstance(tail[0].get("content"), list)
    )


def test_empty_history_compacts_to_nothing_without_error():
    summary, retained = deterministic_summary([])
    assert retained == []
    assert is_empty(summary)
