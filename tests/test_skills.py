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
