from __future__ import annotations

import hashlib
import json

import pytest

from backend.members.adapters import ACTION_KINDS, MemberAction
from backend.members.base_member import build_native_system_prompt, render_skills_block
from backend.members.member_tools import TOOL_ACTIONS, tools_for_category
from backend.skills.store import SkillStore


def _write_package(root, repository, revision, files, *, uploaded=False):
    directory = hashlib.sha256(f"{repository}@{revision}".encode()).hexdigest()
    package = root / directory
    for relative, body in files.items():
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    manifest = {
        "repository": repository,
        "revision": revision,
        "skills": sorted(path for path in files if path.endswith("SKILL.md")),
        "archive_sha256": "0" * 64,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "revision": revision,
        "enabled": True,
        "directory": directory,
        **({"uploaded": True} if uploaded else {}),
    }


def _store(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    packages = {
        "acme/skills": _write_package(
            root,
            "acme/skills",
            "a" * 40,
            {
                "ctf-web/SKILL.md": (
                    "---\n"
                    "name: web-expert\n"
                    "description: Web exploitation playbook for CTF tasks\n"
                    "---\n"
                    "# Web\n\nStart with source review.\n"
                ),
                "ctf-web/notes.txt": "payload notes\n",
                "ctf-web/scripts/async_fuzz.py": "print('fuzz')\n",
                "ctf-pwn/SKILL.md": "# Pwn\n\nCheck the binary mitigations first.\n",
            },
        ),
        "local/house": _write_package(
            root,
            "local/house",
            "b" * 40,
            {
                "house/SKILL.md": (
                    "---\n"
                    "name: house-style\n"
                    "description: Always verify the flag before submitting.\n"
                    "---\n"
                    "# House style\n"
                ),
            },
            uploaded=True,
        ),
    }
    (root / "state.json").write_text(
        json.dumps({"packages": packages}), encoding="utf-8"
    )
    return SkillStore(root)


def test_catalog_selects_by_category_and_always_carries_custom(tmp_path):
    store = _store(tmp_path)

    web = store.catalog("web")
    names = {entry["name"] for entry in web}
    assert "web-expert" in names
    assert "house-style" in names
    assert "ctf-pwn" not in names
    assert next(item for item in web if item["name"] == "web-expert")["description"] == (
        "Web exploitation playbook for CTF tasks"
    )

    pwn = store.catalog("pwn")
    pwn_names = {entry["name"] for entry in pwn}
    assert "ctf-pwn" in pwn_names
    assert "web-expert" not in pwn_names
    assert "house-style" in pwn_names


def test_catalog_falls_back_to_directory_and_first_body_line(tmp_path):
    store = _store(tmp_path)

    entry = next(item for item in store.catalog("pwn") if item["name"] == "ctf-pwn")

    assert entry["path"] == "ctf-pwn/SKILL.md"
    assert entry["description"] == "Check the binary mitigations first."


def test_read_skill_returns_body_and_confined_resource(tmp_path):
    store = _store(tmp_path)

    body = store.read_skill("web-expert")
    assert body["path"] == "ctf-web/SKILL.md"
    assert "Start with source review." in body["content"]

    notes = store.read_skill("ctf-web", "notes.txt")
    assert notes["content"].strip() == "payload notes"

    script = store.read_skill("ctf-web", "scripts/async_fuzz.py")
    assert script["content"].strip() == "print('fuzz')"

    with pytest.raises(ValueError, match="invalid skill resource path"):
        store.read_skill("web-expert", "../ctf-pwn/SKILL.md")
    with pytest.raises(KeyError, match="unknown skill"):
        store.read_skill("does-not-exist")


def test_skill_read_is_a_provider_tool_and_a_member_action():
    names = {tool["name"] for tool in tools_for_category("web")}
    assert "skill_read" in names
    assert TOOL_ACTIONS["skill_read"] == "skill_read"
    assert "skill_read" in ACTION_KINDS

    action = MemberAction.from_obj(
        {"action": "skill_read", "name": "web-expert", "path": "SKILL.md"}
    )
    assert action.kind == "skill_read"
    assert action.args["name"] == "web-expert"


def test_native_prompt_injects_catalog_and_usage_hint(tmp_path):
    store = _store(tmp_path)
    entries = store.catalog("web")

    block = render_skills_block(entries, "web")
    assert "Skills assigned to this web task:" in block
    assert "web-expert" in block
    assert "目录式SKILLS可以根据需要查看具体内容" in block
    assert "skill_read" in block

    prompt = build_native_system_prompt("jade", "web solver", entries, "web")
    assert "You are Member jade" in prompt
    assert "web-expert" in prompt
    assert "目录式SKILLS可以根据需要查看具体内容" in prompt

    assert render_skills_block([], "web") == ""
