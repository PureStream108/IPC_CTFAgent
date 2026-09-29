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

FOLDERS: tuple[str, ...] = ("Web", "Pwn", "Crypto", "Reverse", "Misc", "AI", "Custom")
CUSTOM_FOLDER = "Custom"

CATEGORY_FOLDERS: dict[str, str] = {
    "web": "Web",
    "pwn": "Pwn",
    "crypto": "Crypto",
    "reverse": "Reverse",
    "ai": "AI",
    "misc": "Misc",
    "osint": "Misc",
    "forensics": "Misc",
    "malware": "Reverse",
}

_BUNDLED_FOLDERS: dict[str, str] = {
    "ctf-web": "Web",
    "ctf-pwn": "Pwn",
    "ctf-crypto": "Crypto",
    "ctf-reverse": "Reverse",
    "ctf-malware": "Reverse",
    "ctf-ai-ml": "AI",
    "ctf-forensics": "Misc",
    "ctf-misc": "Misc",
    "ctf-osint": "Misc",
    "ctf-writeup": "Misc",
    "solve-challenge": "Misc",
}
_FOLDER_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("web", "Web"),
    ("pwn", "Pwn"),
    ("binary", "Pwn"),
    ("crypto", "Crypto"),
    ("reverse", "Reverse"),
    ("^re$", "Reverse"),
    ("malware", "Reverse"),
    ("ai", "AI"),
    ("ml", "AI"),
    ("llm", "AI"),
)
_UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
MAX_UPLOAD_BYTES = 256 * 1024
_NPX_SPEC = re.compile(
    r"^(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
    r"|https://[A-Za-z0-9._-]+(?::\d{1,5})?/[A-Za-z0-9._/-]+?(?:\.git)?)$"
)


def default_folder_for(directory: str) -> str:
    """Return the folder a skill directory belongs to."""
    name = (directory or "").strip().lower()
    if name in _BUNDLED_FOLDERS:
        return _BUNDLED_FOLDERS[name]
    for pattern, folder in _FOLDER_KEYWORDS:
        if re.search(pattern if pattern.startswith("^") else rf"\b{pattern}\b", name):
            return folder
    return "Misc"


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
        """Skills Diamond distributes for a challenge category.

        Selection follows the Skill browser's folders, so moving a skill in the
        UI changes what Members actually receive. ``Misc`` and ``Custom`` always
        travel along: they hold cross-cutting and operator-authored skills.
        """
        folder = CATEGORY_FOLDERS.get(str(category).strip().lower(), "Misc")
        wanted = {folder, "Misc", CUSTOM_FOLDER}
        grouped = self.tree()
        allowed: dict[str, set[str]] = {}
        for name in wanted:
            for item in grouped.get(name, []):
                if item["enabled"]:
                    allowed.setdefault(item["repository"], set()).add(item["path"])
        return [
            {**p, "skills": sorted(allowed.get(p["repository"], set()))}
            for p in self.snapshot()
            if allowed.get(p["repository"])
        ]

    def _placements(self, state: dict) -> dict:
        return state.setdefault("placements", {})

    def _skill_key(self, repository: str, path: str) -> str:
        return f"{repository}::{path}"

    def _folder_of(self, state: dict, repository: str, path: str, *, uploaded: bool) -> str:
        placed = self._placements(state).get(self._skill_key(repository, path))
        if placed in FOLDERS:
            return placed
        if uploaded:
            return CUSTOM_FOLDER
        return default_folder_for(Path(path).parent.name)

    def skill_name(self, path: str) -> str:
        """Display name for a skill: its directory, or the file stem at the root."""
        parent = Path(path).parent.name
        return parent or Path(path).stem

    def tree(self) -> dict[str, list[dict]]:
        """Return installed skills grouped by folder, for the Skill browser."""
        with _LOCK:
            state = self._state()
            grouped: dict[str, list[dict]] = {folder: [] for folder in FOLDERS}
            for repository, info in state["packages"].items():
                uploaded = bool(info.get("uploaded"))
                described = self._describe(repository, info)
                for path in described["skills"]:
                    folder = self._folder_of(state, repository, path, uploaded=uploaded)
                    grouped.setdefault(folder, []).append({
                        "name": self.skill_name(path),
                        "repository": repository,
                        "revision": described["revision"],
                        "path": path,
                        "folder": folder,
                        "enabled": described["enabled"],
                        "uploaded": uploaded,
                    })
            for items in grouped.values():
                items.sort(key=lambda item: item["name"].lower())
            return grouped

    def move(self, repository: str, path: str, folder: str) -> dict[str, list[dict]]:
        """Reassign one skill to another folder (drag-and-drop in the UI)."""
        if folder not in FOLDERS:
            raise ValueError(f"folder must be one of {', '.join(FOLDERS)}")
        with _LOCK:
            state = self._state()
            info = state["packages"].get(repository)
            if info is None:
                raise KeyError(repository)
            if path not in self._describe(repository, info)["skills"]:
                raise KeyError(path)
            self._placements(state)[self._skill_key(repository, path)] = folder
            self._save(state)
        return self.tree()

    def upload(self, name: str, content: str) -> dict:
        """Store an operator-provided SKILL.md under the Custom folder."""
        label = (name or "").strip()
        if label.lower().endswith(".md"):
            label = label[:-3]
        if not _UPLOAD_NAME.fullmatch(label):
            raise ValueError("use a short name of letters, numbers, spaces, dots, dashes or underscores")
        if not content.strip():
            raise ValueError("SKILL.md content is empty")
        if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
            raise ValueError("SKILL.md exceeds 256 KiB")
        directory = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-.") or "skill"
        repository = f"local/{directory}"
        if _PACKAGE.fullmatch(repository) is None:
            raise ValueError("derived skill name is not usable")
        revision = hashlib.sha256(content.encode("utf-8")).hexdigest()[:40].ljust(40, "0")
        key = hashlib.sha256(f"{repository}@{revision}".encode()).hexdigest()
        target = self.root / key
        relative = f"{directory}/SKILL.md"
        with _LOCK:
            state = self._state()
            if repository in state["packages"]:
                raise ValueError(f"a skill named {label} already exists")
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                stage = Path(temporary) / "package"
                (stage / directory).mkdir(parents=True)
                (stage / directory / "SKILL.md").write_text(content, encoding="utf-8")
                manifest = {
                    "repository": repository, "revision": revision, "skills": [relative],
                    "archive_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                stage.replace(target)
            state["packages"][repository] = {
                "revision": revision, "enabled": True, "directory": key, "uploaded": True,
            }
            self._placements(state)[self._skill_key(repository, relative)] = CUSTOM_FOLDER
            self._save(state)
            return self._describe(repository, state["packages"][repository])

    def npx_spec(self, value: str) -> str:
        """Validate the argument for ``npx skills add``.

        The value reaches a subprocess, so only an owner/repo slug or an HTTPS
        git URL is accepted and it is never passed through a shell.
        """
        spec = (value or "").strip()
        if not spec:
            raise ValueError("enter owner/repository or a .git URL")
        if len(spec) > 200 or not _NPX_SPEC.fullmatch(spec):
            raise ValueError("enter owner/repository or an https://…/repo.git URL")
        return spec

    def install_with_npx(self, spec: str, *, timeout: int = 300) -> dict:
        """Run ``npx skills add <spec>`` so the CLI's own resolution is used."""
        import subprocess

        spec = self.npx_spec(spec)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                ["npx", "--yes", "skills", "add", spec],
                cwd=str(self.root), capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("npx is not available in this deployment") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"npx skills add timed out after {timeout}s") from exc
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            raise RuntimeError(f"npx skills add failed: {output[-2000:] or completed.returncode}")
        return {"spec": spec, "output": output[-4000:], "tree": self.tree()}
