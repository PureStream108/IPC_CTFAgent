from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

import requests

DEFAULT_REPOSITORY = "ljagiello/ctf-skills"
DEFAULT_REVISION = "c332c7be1b27cb64639a20124ac55ba916adef92"
_PACKAGE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[a-f0-9]{40}$")
_LOCK = threading.RLock()
_CATEGORIES = {"ai": "ctf-ai-ml", "forensics": "ctf-forensics", "malware": "ctf-malware"}


class SkillStore:
    def __init__(self, root: Path, bundled: Path | None = None):
        self.root = Path(root)
        self.bundled = Path(bundled) if bundled else None
        self.root.mkdir(parents=True, exist_ok=True)

    def _state(self):
        path = self.root / "state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"packages": {}}

    def _save(self, state):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as handle:
            json.dump(state, handle, ensure_ascii=False)
            temporary = handle.name
        os.replace(temporary, self.root / "state.json")

    def bootstrap(self):
        """Copy the build-pinned package once; startup never needs the network."""
        if not self.bundled or not self.bundled.is_dir():
            return
        with _LOCK:
            state = self._state()
            if DEFAULT_REPOSITORY in state["packages"]:
                return
            for manifest in self.bundled.glob("*/manifest.json"):
                info = json.loads(manifest.read_text(encoding="utf-8"))
                if info.get("repository") != DEFAULT_REPOSITORY or info.get("revision") != DEFAULT_REVISION:
                    continue
                target = self.root / manifest.parent.name
                if not target.exists():
                    shutil.copytree(manifest.parent, target)
                state["packages"][DEFAULT_REPOSITORY] = {"revision": DEFAULT_REVISION, "enabled": True, "directory": target.name}
                self._save(state)
                return

    def install(self, repository: str, revision: str) -> dict:
        if not _PACKAGE.fullmatch(repository) or not _REVISION.fullmatch(revision):
            raise ValueError("use owner/repository and a full 40-character commit hash")
        key = hashlib.sha256(f"{repository}@{revision}".encode()).hexdigest()
        target = self.root / key
        with _LOCK:
            if not target.exists():
                with requests.get(f"https://codeload.github.com/{repository}/zip/{revision}", stream=True, timeout=(10, 60)) as response:
                    response.raise_for_status()
                    archive = io.BytesIO()
                    for chunk in response.iter_content(1024 * 1024):
                        archive.write(chunk)
                        if archive.tell() > 32 * 1024 * 1024:
                            raise ValueError("skill archive exceeds 32 MiB")
                with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                    stage = Path(temporary) / "package"
                    stage.mkdir()
                    with zipfile.ZipFile(archive) as zipped:
                        entries = zipped.infolist()
                        if len(entries) > 10000 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
                            raise ValueError("skill archive expands beyond its resource limit")
                        for entry in entries:
                            parts = entry.filename.replace("\\", "/").split("/")
                            if len(parts) < 2 or ".." in parts or any(":" in p for p in parts):
                                raise ValueError("invalid archive path")
                            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                                raise ValueError("skill archives cannot contain symbolic links")
                            if entry.is_dir():
                                continue
                            destination = stage.joinpath(*parts[1:])
                            if not destination.resolve().is_relative_to(stage.resolve()):
                                raise ValueError("archive path escapes package")
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            destination.write_bytes(zipped.read(entry))
                    skills = sorted(str(p.relative_to(stage)).replace("\\", "/") for p in stage.rglob("SKILL.md"))
                    if not skills:
                        raise ValueError("repository contains no SKILL.md")
                    manifest = {"repository": repository, "revision": revision, "skills": skills,
                                "archive_sha256": hashlib.sha256(archive.getvalue()).hexdigest()}
                    (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                    stage.replace(target)
            state = self._state()
            state["packages"][repository] = {"revision": revision, "enabled": True, "directory": key}
            self._save(state)
            return self._describe(repository, state["packages"][repository])

    def _describe(self, repository, info):
        manifest = json.loads((self.root / info["directory"] / "manifest.json").read_text(encoding="utf-8"))
        return {"repository": repository, "revision": info["revision"], "enabled": info["enabled"],
                "skills": manifest["skills"], "archive_sha256": manifest["archive_sha256"]}

    def packages(self):
        with _LOCK:
            return [self._describe(repo, info) for repo, info in self._state()["packages"].items()]

    def enable(self, repository: str, enabled: bool):
        with _LOCK:
            state = self._state()
            if repository not in state["packages"]:
                raise KeyError(repository)
            state["packages"][repository]["enabled"] = enabled
            self._save(state)

    def snapshot(self) -> list[dict]:
        return [p for p in self.packages() if p["enabled"]]

    def read(self, repository: str, revision: str, path: str) -> str:
        if not _PACKAGE.fullmatch(repository) or not _REVISION.fullmatch(revision):
            raise ValueError("invalid skill identity")
        key = hashlib.sha256(f"{repository}@{revision}".encode()).hexdigest()
        root = (self.root / key).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml"}:
            raise ValueError("invalid skill resource path")
        if target.stat().st_size > 256 * 1024:
            raise ValueError("skill resource exceeds 256 KiB")
        return target.read_text(encoding="utf-8")

    def for_category(self, category: str) -> list[dict]:
        preferred = {_CATEGORIES.get(category, f"ctf-{category}"), "solve-challenge"}
        return [{**p, "skills": [s for s in p["skills"] if Path(s).parent.name in preferred]}
                for p in self.snapshot()]
