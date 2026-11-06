from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.compaction import fit_history, summarize
from backend.members.adapters import MemberAction
from backend.members.base_member import BaseMember, MemberDeps
from backend.members.member_tools import MEMBER_TOOLS, SKILL_READ_LIMIT
from backend.sandbox.output import clip
from backend.sandbox.sandbox import LocalSandbox
from backend.tools.tool_registry import Tool


def test_forty_megabyte_output_is_spooled_and_fully_readable(tmp_path):
    sandbox = LocalSandbox("output-budget", tmp_path)
    result = sandbox.exec(f'"{sys.executable}" -c "import sys;sys.stdout.buffer.write(b\'x\'*(40*1024*1024))"')
    assert result.ok
    assert len(result.stdout.encode()) < 16384
    artifact = result.artifacts["stdout"]
    assert artifact["bytes"] == 40 * 1024 * 1024
    path = Path(artifact["path"])
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == artifact["sha256"]
    first = sandbox.read_page(str(path), limit=8192)
    last = sandbox.read_page(str(path), offset=artifact["bytes"]-8, limit=8)
    assert first["data"] == "x" * 8192
    assert first["next_offset"] == 8192
    assert last["data"] == "x" * 8
    assert last["next_offset"] == artifact["bytes"]
    with pytest.raises(ValueError):
        sandbox.read_page("../../outside")


def test_large_recent_result_cannot_survive_the_context_budget():
    history = [{"role": "user", "content": "Investigate the attachment"},
               {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "x" * (40 * 1024 * 1024)}]
    projection = fit_history(history, max_bytes=16384, max_tokens=4000)
    assert len(json.dumps(projection).encode()) <= 16384
    assert projection[0]["content"] == history[0]["content"]
    assert all(item.get("role") != "tool" or any(
        call.get("id") == item.get("tool_call_id") for message in projection
        for call in message.get("tool_calls", [])
    ) for item in projection)


def test_compaction_failure_reports_its_reason_and_preserves_artifact_refs():
    reasons = []
    artifact = {"path": "/workspace/shared/result.log", "sha256": "a" * 64}
    history = [{"role": "user", "content": "task"}, {"role": "tool", "content": json.dumps({"process": {"artifacts": {"stdout": artifact}}})},
               *[{"role": "user", "content": str(i)} for i in range(20)]]
    def unavailable(_prompt, _history):
        raise TimeoutError("summary deadline")
    summary, _tail = summarize(history, request=unavailable, on_degraded=reasons.append)
    assert reasons == ["TimeoutError: summary deadline"]
    assert artifact in summary["artifacts"]


# --- section 8: explicit truncation, skill_read TOC and list_tools paging ---


class _Logger:
    def __init__(self):
        self.events = []

    def project(self, event, *args, **kwargs):
        self.events.append((event, args, kwargs))

    def tool(self, *args, **kwargs):
        self.events.append((args[0] if args else "tool", args, kwargs))


class _Adapter:
    config = SimpleNamespace(api_format="mock")

    def turn(self, *args):
        return None


def _member(**changes):
    logger = changes.pop("logger", _Logger())
    values = {
        "db": None,
        "logger": logger,
        "sandbox": SimpleNamespace(),
        "mcps": SimpleNamespace(names=list),
        "registry": SimpleNamespace(search=lambda *_args, **_kwargs: []),
        "memory": None,
        "competition_store": None,
        "competition_assignment": None,
    }
    values.update(changes)
    return BaseMember("amber", _Adapter(), MemberDeps(**values)), logger


class _SkillStore:
    def __init__(self, content, *, name="web-expert", path="ctf-web/SKILL.md"):
        self.content = content
        self.name = name
        self.path = path

    def read_skill(self, name, relative, category=None):
        del name, relative, category
        return {
            "name": self.name,
            "path": self.path,
            "content": self.content,
            "repository": "acme/skills",
            "revision": "a" * 40,
        }


def _tool_registry(count, *, when_to_use="Use it for the task."):
    tools = [
        Tool(
            name=f"tool-{index:02d}",
            category="web",
            description=f"description {index}",
            path=f"/usr/local/bin/tool-{index:02d}",
            exec=f"tool-{index:02d} --help",
            tags=["web"],
            when_to_use=when_to_use,
        )
        for index in range(count)
    ]

    class Registry:
        def exposed_for(self, category, available_mcps=None):
            del category, available_mcps
            return list(tools)

        def get(self, name, available_mcps=None):
            del available_mcps
            return next((tool for tool in tools if tool.name == name), None)

    return Registry()


def _truncation(marker):
    omitted, total = re.search(r"truncated (\d+) chars of (\d+)", marker).groups()
    return int(omitted), int(total)


def test_clip_keeps_head_and_tail_and_reports_the_omitted_count():
    body = "H" * 100 + "M" * 1800 + "T" * 100
    out = clip(body, 400, hint="call skill_read with offset=123")
    assert len(out) <= 400
    assert out.startswith("H" * 100)
    assert out.endswith("T" * 100)
    marker = re.search(r"\n\.\.\.\[truncated \d+ chars of \d+[^\]]*\]\n", out)
    assert marker
    omitted, total = _truncation(marker.group(0))
    assert total == len(body)
    assert omitted == len(body) - (len(out) - len(marker.group(0)))
    assert "call skill_read with offset=123" in marker.group(0)
    assert clip("short body", 400) == "short body"
    # A budget smaller than the marker still reports the whole omission.
    assert "truncated 50 chars of 50" in clip("x" * 50, 10)


def test_clip_is_multibyte_safe():
    body = "前缀" * 400 + "BODY_SECRET" + "后缀" * 400
    out = clip(body, 320, hint="继续读取")
    assert len(out) <= 320
    assert "\ufffd" not in out
    assert out.encode("utf-8").decode("utf-8") == out
    marker = re.search(r"\n\.\.\.\[truncated \d+ chars of \d+[^\]]*\]\n", out)
    assert marker
    omitted, total = _truncation(marker.group(0))
    assert total == len(body)
    assert omitted == len(body) - (len(out) - len(marker.group(0)))


def test_skill_read_returns_a_table_of_contents_and_a_real_continuation_offset():
    filler = "A" * 33_000
    fence = chr(96) * 3
    content = (
        "# Web skill\n\nIntro line.\n\n"
        f"{fence}bash\n# fenced comment\n{fence}\n\n"
        "## SQL injection\n\n" + filler + "\n\n"
        "### Time based\n\nSLEEP check\n\n"
        "## XSS\n\npayload"
    )
    solver, _logger = _member(skills=_SkillStore(content))
    first = solver._dispatch_skill_read(
        "proj_001",
        "intent-1",
        "web",
        MemberAction(kind="skill_read", args={"name": "web-expert"}),
    )
    text = first.observation
    assert first.invalid_action is False
    assert text.startswith("[skill:web-expert] ctf-web/SKILL.md")
    assert f"chars 0-{SKILL_READ_LIMIT}/{len(content)}" in text
    assert "Table of contents (Markdown headings):" in text
    for heading in ("## SQL injection", "### Time based", "## XSS"):
        assert f"{heading} @ {content.index(heading)}" in text
    # Comments inside a fenced block are not Markdown headings.
    assert "# fenced comment @ " not in text
    assert f"call skill_read with offset={SKILL_READ_LIMIT}" in text
    assert content[:SKILL_READ_LIMIT][-100:] in text

    # The checkpoint keeps the locator only, never the body.
    assert solver.observations[-1] == (
        "[skill_read] skill:web-expert path:ctf-web/SKILL.md "
        f"offset:0 chars:{len(content)}"
    )
    assert filler[:200] not in solver.observations[-1]

    second = solver._dispatch_skill_read(
        "proj_001",
        "intent-1",
        "web",
        MemberAction(
            kind="skill_read",
            args={"name": "web-expert", "offset": SKILL_READ_LIMIT},
        ),
    )
    second_text = second.observation
    assert "Table of contents" not in second_text
    assert second_text.startswith(
        f"[skill:web-expert] ctf-web/SKILL.md "
        f"(chars {SKILL_READ_LIMIT}-{len(content)}/{len(content)})"
    )
    assert content[SKILL_READ_LIMIT:] in second_text


def test_list_tools_returns_valid_json_and_pages_to_completion():
    solver, _logger = _member(registry=_tool_registry(25))
    first = json.loads(
        solver._dispatch_tool_lookup(
            "proj", "web", MemberAction(kind="list_tools", args={})
        ).observation
    )
    assert first["total"] == 25
    assert len(first["items"]) == 20
    assert first["next_offset"] == 20

    seen = [item["name"] for item in first["items"]]
    offset = first["next_offset"]
    pages = 1
    while offset is not None:
        page = json.loads(
            solver._dispatch_tool_lookup(
                "proj",
                "web",
                MemberAction(kind="list_tools", args={"offset": offset}),
            ).observation
        )
        assert page["total"] == 25
        seen.extend(item["name"] for item in page["items"])
        offset = page["next_offset"]
        pages += 1
        assert pages <= 5
    assert pages == 2
    assert seen == [f"tool-{index:02d}" for index in range(25)]


def test_list_tools_limit_defaults_to_twenty_and_is_bounded():
    solver, _logger = _member(registry=_tool_registry(25))

    def page(args):
        result = solver._dispatch_tool_lookup(
            "proj", "web", MemberAction(kind="list_tools", args=args)
        )
        return json.loads(result.observation)

    assert len(page({})["items"]) == 20
    seven = page({"limit": 7})
    assert len(seven["items"]) == 7
    assert seven["next_offset"] == 7
    assert len(page({"limit": 500})["items"]) == 25


def test_list_tools_shrinks_an_oversized_page_instead_of_cutting_json():
    solver, _logger = _member(
        registry=_tool_registry(25, when_to_use="x" * 1200)
    )
    seen = []
    offset = 0
    pages = 0
    while offset is not None:
        payload = json.loads(
            solver._dispatch_tool_lookup(
                "proj",
                "web",
                MemberAction(kind="list_tools", args={"offset": offset, "limit": 50}),
            ).observation
        )
        assert payload["items"]
        assert payload["next_offset"] in (None, offset + len(payload["items"]))
        seen.extend(item["name"] for item in payload["items"])
        offset = payload["next_offset"]
        pages += 1
        assert pages <= 10
    assert pages > 1
    assert len(seen) == len(set(seen)) == 25


def test_list_tools_schema_exposes_paging_and_skill_read_limit_is_raised():
    tools = {tool["name"]: tool for tool in MEMBER_TOOLS}
    list_properties = tools["list_tools"]["parameters"]["properties"]
    assert list_properties["offset"]["minimum"] == 0
    assert list_properties["limit"]["minimum"] == 1
    assert list_properties["limit"]["maximum"] == 50
    assert SKILL_READ_LIMIT == 32_000
    skill_properties = tools["skill_read"]["parameters"]["properties"]
    assert skill_properties["limit"]["maximum"] == SKILL_READ_LIMIT
