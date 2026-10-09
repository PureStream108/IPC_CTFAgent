import io
import zipfile

import pytest

from backend.skills.store import DEFAULT_REPOSITORY, DEFAULT_REVISION, SkillStore


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


def test_moving_a_skill_changes_what_diamond_distributes(tmp_path, monkeypatch):
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


def test_install_with_npx_runs_the_cli_without_a_shell(tmp_path, monkeypatch):
    recorded = {}

    class Completed:
        returncode = 0
        stdout = "added ctf-skills"
        stderr = ""

    def fake_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)
    store = SkillStore(tmp_path / "installed")
    result = store.install_with_npx("ljagiello/ctf-skills")
    assert recorded["command"] == ["npx", "--yes", "skills", "add", "ljagiello/ctf-skills"]
    assert "shell" not in recorded["kwargs"]
    assert "added ctf-skills" in result["output"]
