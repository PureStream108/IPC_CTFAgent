import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.skills.store import (
    DEFAULT_REPOSITORY,
    DEFAULT_REVISION,
    FOLDERS,
    SkillStore,
)


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return stream.getvalue()


class Response:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.content


def test_versioned_install_offline_bootstrap_and_disable(tmp_path, monkeypatch):
    content = archive({"repo/ctf-web/SKILL.md": "# Web\nEvidence first", "repo/LICENSE": "license"})
    calls = []
    monkeypatch.setattr("backend.skills.store.requests.get", lambda *a, **kw: calls.append(a[0]) or Response(content))
    bundled = SkillStore(tmp_path / "bundled")
    bundled.install(DEFAULT_REPOSITORY, DEFAULT_REVISION)
    installed = SkillStore(tmp_path / "installed", tmp_path / "bundled")
    installed.bootstrap()
    installed.bootstrap()
    assert len(calls) == 1
    assert installed.for_category("web")[0]["skills"] == ["ctf-web/SKILL.md"]
    installed.enable(DEFAULT_REPOSITORY, False)
    assert installed.snapshot() == []
    assert "Evidence" in installed.read(DEFAULT_REPOSITORY, DEFAULT_REVISION, "ctf-web/SKILL.md")
    installed.install(DEFAULT_REPOSITORY, DEFAULT_REVISION)
    assert len(calls) == 1
    assert installed.snapshot()

def test_skill_archive_path_escape_is_rejected(tmp_path, monkeypatch):
    content = archive({"repo/../../escape/SKILL.md": "bad"})
    monkeypatch.setattr("backend.skills.store.requests.get", lambda *a, **kw: Response(content))
    store = SkillStore(tmp_path / "installed")
    with pytest.raises(ValueError, match="path"):
        store.install(DEFAULT_REPOSITORY, DEFAULT_REVISION)
    assert store.packages() == []


def test_install_requires_immutable_commit(tmp_path):
    with pytest.raises(ValueError, match="commit"):
        SkillStore(tmp_path).install(DEFAULT_REPOSITORY, "main")


def _sorted_bundle(tmp_path, monkeypatch):
    content = archive({
        "repo/ctf-web/SKILL.md": "# Web",
        "repo/ctf-pwn/SKILL.md": "# Pwn",
        "repo/ctf-crypto/SKILL.md": "# Crypto",
        "repo/ctf-reverse/SKILL.md": "# Reverse",
        "repo/ctf-ai-ml/SKILL.md": "# AI",
        "repo/ctf-misc/SKILL.md": "# Misc",
        "repo/solve-challenge/SKILL.md": "# Solve",
    })
    monkeypatch.setattr("backend.skills.store.requests.get", lambda *a, **kw: Response(content))
    store = SkillStore(tmp_path / "installed")
    store.install(DEFAULT_REPOSITORY, DEFAULT_REVISION)
    return store


def test_bundled_package_is_pre_sorted_by_challenge_type(tmp_path, monkeypatch):
    tree = _sorted_bundle(tmp_path, monkeypatch).tree()
    assert [item["name"] for item in tree["Web"]] == ["ctf-web"]
    assert [item["name"] for item in tree["Pwn"]] == ["ctf-pwn"]
    assert [item["name"] for item in tree["Crypto"]] == ["ctf-crypto"]
    assert [item["name"] for item in tree["Reverse"]] == ["ctf-reverse"]
    assert [item["name"] for item in tree["AI"]] == ["ctf-ai-ml"]
    assert sorted(item["name"] for item in tree["Misc"]) == ["ctf-misc", "solve-challenge"]
    assert tree["Custom"] == []


def test_uploaded_skill_lands_in_custom_and_reaches_every_category(tmp_path, monkeypatch):
    store = _sorted_bundle(tmp_path, monkeypatch)
    store.upload("House Style.md", "# House style\nalways verify the flag")
    assert [item["name"] for item in store.tree()["Custom"]] == ["House-Style"]
    for category in ("web", "pwn", "crypto", "ai", "misc"):
        distributed = {s for package in store.for_category(category) for s in package["skills"]}
        assert "House-Style/SKILL.md" in distributed
    with pytest.raises(ValueError, match="already exists"):
        store.upload("House Style.md", "# duplicate")
    with pytest.raises(ValueError, match="empty"):
        store.upload("Blank", "   ")


def test_moving_a_skill_changes_what_assignments_receive(tmp_path, monkeypatch):
    store = _sorted_bundle(tmp_path, monkeypatch)
    assert "ctf-pwn/SKILL.md" in {
        s for package in store.for_category("pwn") for s in package["skills"]
    }
    store.move(DEFAULT_REPOSITORY, "ctf-pwn/SKILL.md", "Web")
    assert "ctf-pwn/SKILL.md" in {
        s for package in store.for_category("web") for s in package["skills"]
    }
    assert "ctf-pwn/SKILL.md" not in {
        s for package in store.for_category("pwn") for s in package["skills"]
    }
    with pytest.raises(ValueError, match="folder"):
        store.move(DEFAULT_REPOSITORY, "ctf-web/SKILL.md", "Nonexistent")
    with pytest.raises(KeyError):
        store.move(DEFAULT_REPOSITORY, "ctf-absent/SKILL.md", "Web")


@pytest.mark.parametrize(
    "spec",
    ["", "rm -rf /", "a b", "http://x/y.git", "$(whoami)", "x/y; ls", "../../etc/passwd"],
)
def test_npx_spec_rejects_anything_but_a_slug_or_https_git_url(tmp_path, spec):
    with pytest.raises(ValueError):
        SkillStore(tmp_path).npx_spec(spec)


@pytest.mark.parametrize(
    "spec",
    ["ljagiello/ctf-skills", "https://github.com/ljagiello/ctf-skills.git"],
)
def test_npx_spec_accepts_a_slug_and_an_https_git_url(tmp_path, spec):
    assert SkillStore(tmp_path).npx_spec(spec) == spec


def test_install_with_npx_imports_the_cli_output(tmp_path, monkeypatch):
    recorded = {}

    class Completed:
        returncode = 0
        stdout = "added ctf-skills"
        stderr = ""

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        skill = Path(kwargs["cwd"]) / ".agents" / "skills" / "ctf-web"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: ctf-web\ndescription: Web notes\n---\n# Web\n\nSource review.\n",
            encoding="utf-8",
        )
        (skill / "notes.txt").write_text("payload notes\n", encoding="utf-8")
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("backend.skills.store.shutil.which", lambda name: "npx")
    store = SkillStore(tmp_path / "installed")
    result = store.install_with_npx("ljagiello/ctf-skills")

    assert recorded["command"] == [
        "npx", "--yes", "skills", "add", "ljagiello/ctf-skills",
        "--json", "-y", "--copy", "--skill", "*",
    ]
    assert "shell" not in recorded["kwargs"]
    assert "added ctf-skills" in result["output"]
    assert result["repository"] == "ljagiello/ctf-skills"
    assert any(item["name"] == "ctf-web" for item in store.tree()["Web"])
    assert "Source review." in store.read_skill("ctf-web")["content"]


def test_install_with_npx_uses_json_path_when_canonical_copy_is_missing(tmp_path, monkeypatch):
    class Completed:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        skill = Path(kwargs["cwd"]) / ".claude" / "skills" / "custom-one"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Custom\n\nUse the custom path.\n", encoding="utf-8")
        Completed.stdout = json.dumps([
            {"name": "custom-one", "path": str(skill), "source": "acme/skills"}
        ])
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("backend.skills.store.shutil.which", lambda name: "npx")
    store = SkillStore(tmp_path / "installed")
    result = store.install_with_npx("acme/skills")

    assert result["repository"] == "acme/skills"
    assert any(item["name"] == "custom-one" for item in store.tree()["Misc"])
    assert "Use the custom path." in store.read_skill("custom-one")["content"]


def test_install_with_npx_reports_a_failed_cli(tmp_path, monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "clone failed"

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Failed())
    monkeypatch.setattr("backend.skills.store.shutil.which", lambda name: "npx")
    store = SkillStore(tmp_path / "installed")

    with pytest.raises(RuntimeError, match="clone failed"):
        store.install_with_npx("ljagiello/ctf-skills")
    assert store.packages() == []


def test_default_store_has_an_empty_catalog_and_rejects_reads(tmp_path):
    store = SkillStore(tmp_path / "skills")
    assert store.bundled is None

    # A store without a bundled source must not install or copy anything.
    store.bootstrap()
    assert not (tmp_path / "skills" / "state.json").exists()
    assert store.packages() == []
    assert store.snapshot() == []
    assert store.tree() == {folder: [] for folder in FOLDERS}
    for category in ("web", "pwn", "web+pwn", "misc"):
        assert store.catalog(category) == []
    with pytest.raises((KeyError, ValueError)):
        store.read_skill("ctf-web")


class _SkillReadAdapter:
    config = SimpleNamespace(api_format="mock")

    def turn(self, *args):
        return None


class _SkillReadLogger:
    def project(self, *args, **kwargs):
        pass

    def tool(self, *args, **kwargs):
        pass


def _skill_read_member(**overrides):
    from backend.members.base_member import BaseMember, MemberDeps

    values = {
        "db": None,
        "logger": _SkillReadLogger(),
        "sandbox": SimpleNamespace(),
        "mcps": SimpleNamespace(names=list),
        "registry": SimpleNamespace(search=lambda *args, **kwargs: []),
        "memory": None,
    }
    values.update(overrides)
    return BaseMember("amber", _SkillReadAdapter(), MemberDeps(**values))


def test_skill_read_deployment_notice_only_when_the_store_is_absent(tmp_path):
    from backend.members.adapters import MemberAction

    action = MemberAction(kind="skill_read", args={"name": "ctf-web"})

    absent = _skill_read_member()._dispatch_skill_read(
        "proj_001", "intent-1", "web+pwn", action
    )
    assert absent.invalid_action is True
    assert "skills are not installed" in absent.observation

    # An empty store still exists: its catalogue is empty, so the read fails as
    # an unknown skill rather than claiming the deployment has no skill system.
    store = SkillStore(tmp_path / "skills")
    assert store.catalog("web+pwn") == []
    empty = _skill_read_member(skills=store)._dispatch_skill_read(
        "proj_001", "intent-1", "web+pwn", action
    )
    assert empty.invalid_action is True
    assert "skills are not installed" not in empty.observation
    assert "unknown skill" in empty.observation


def test_web_pwn_assignments_see_both_skill_families(tmp_path):
    """The union category must receive Web and Pwn skills, not just Misc."""
    store = SkillStore(tmp_path / "skills")
    assert store._folders_for("web+pwn") == ["Web", "Pwn", "Misc", "Custom"]
    assert store._folders_for("web") == ["Web", "Misc", "Custom"]
    assert store._folders_for("pwn") == ["Pwn", "Misc", "Custom"]
