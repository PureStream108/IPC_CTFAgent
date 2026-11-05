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
import yaml

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
    "web+pwn": "Web",
}

# A category that legitimately spans two folders. A remote challenge with no
# usable classification can be either, so it receives both families.
_CATEGORY_FOLDER_GROUPS: dict[str, tuple[str, ...]] = {
    "web+pwn": ("Web", "Pwn"),
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


def skill_frontmatter(text: str) -> dict[str, str]:
    """Read the `name` and `description` from a SKILL.md YAML header.

    Upstream skill packages use frontmatter as the human-facing label; the
    directory name is only a fallback. A malformed header must not make the
    skill unreadable, so this returns an empty mapping instead of raising.
    """
    if not isinstance(text, str) or not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next(
        (index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}),
        None,
    )
    if end is None:
        return {}
    try:
        data = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("name", "description"):
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            normalized = " ".join(str(value).split())
            if normalized:
                result[key] = normalized
    return result


def skill_summary(text: str, *, limit: int = 240) -> str:
    """Return one readable line from a SKILL.md body when it has no header."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next(
            (index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}),
            None,
        )
        if end is not None:
            lines = lines[end + 1:]
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```")):
            continue
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^>\s*", "", stripped)
        return stripped[:limit]
    return ""


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
        """Package view of the skills selected for a challenge category.

        Kept for the MCP `discover` tool; task assignment uses
        :meth:`catalog` for the compact Member-facing list. Both follow the
        Skill browser's folders, so moving a skill in the UI changes what the
        next assignment receives. ``Misc`` and ``Custom`` always travel along:
        they hold cross-cutting and operator-authored skills.
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

    def _folders_for(self, category: str) -> list[str]:
        """Folder priority for one challenge category (specific, Misc, Custom)."""
        key = str(category).strip().lower()
        wanted = list(_CATEGORY_FOLDER_GROUPS.get(key, (CATEGORY_FOLDERS.get(key, "Misc"),)))
        for name in ("Misc", CUSTOM_FOLDER):
            if name not in wanted:
                wanted.append(name)
        return wanted

    def _entries(self, category: str | None = None) -> list[dict]:
        """Enabled skill entries for a category, without reading their bodies."""
        grouped = self.tree()
        folders = self._folders_for(category) if category is not None else list(grouped)
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for folder in folders:
            for item in grouped.get(folder, []):
                key = (item["repository"], item["path"])
                if key in seen or not item["enabled"]:
                    continue
                seen.add(key)
                entries.append(item)
        return entries

    def _named_entries(self, category: str | None = None):
        """Yield (entry, body, frontmatter, display_name) for selected skills."""
        for item in self._entries(category):
            try:
                body = self.read(item["repository"], item["revision"], item["path"])
            except (OSError, ValueError):
                body = ""
            meta = skill_frontmatter(body)
            name = (meta.get("name") or item["name"]).strip()
            yield item, body, meta, name

    def catalog(self, category: str) -> list[dict]:
        """Compact skill catalogue injected when a task is assigned.

        Only metadata travels with the assignment: the SKILL.md body stays on
        disk and is read on demand through :meth:`read_skill`. Selection is
        folder-based, so moving a skill in the UI changes what the next
        assignment of that category receives.
        """
        wanted = self._folders_for(category)
        entries: list[dict] = []
        for item, body, meta, name in self._named_entries(category):
            description = (meta.get("description") or skill_summary(body)).strip()
            entries.append({
                "name": name,
                "description": description[:240],
                "path": item["path"],
                "repository": item["repository"],
                "revision": item["revision"],
                "folder": item["folder"],
            })
        entries.sort(key=lambda entry: (
            wanted.index(entry["folder"]) if entry["folder"] in wanted else len(wanted),
            entry["name"].lower(),
        ))
        return entries

    def read_skill(
        self, name: str, relative: str = "SKILL.md", category: str | None = None
    ) -> dict:
        """Read one injected skill's body or a text resource in its directory.

        `name` accepts the frontmatter name, the directory name, or the
        package-relative path. `relative` is confined to that one skill
        directory; traversal and non-text resources are rejected.
        """
        raw = (name or "").strip().replace("\\", "/").strip("/")
        if not raw:
            raise ValueError("a skill name is required")
        matches: list[tuple[dict, str]] = []
        for item, _body, _meta, display_name in self._named_entries(category):
            directory = str(Path(item["path"]).parent).replace("\\", "/").strip("/")
            if raw in {display_name, item["name"], item["path"], directory}:
                matches.append((item, display_name))
        if not matches:
            raise KeyError(f"unknown skill: {raw}")
        if len(matches) > 1:
            choices = ", ".join(
                sorted(f"{item['repository']}:{item['path']}" for item, _ in matches)
            )
            raise ValueError(f"ambiguous skill {raw!r}; choose one of: {choices}")
        item, display_name = matches[0]
        directory = str(Path(item["path"]).parent).replace("\\", "/").strip("/")
        parts = [
            part for part in (relative or "SKILL.md").strip().replace("\\", "/").split("/")
            if part not in {"", "."}
        ]
        if not parts or any(part == ".." or ":" in part for part in parts):
            raise ValueError("invalid skill resource path")
        relative_path = "/".join(parts)
        key = hashlib.sha256(f"{item['repository']}@{item['revision']}".encode()).hexdigest()
        package_root = (self.root / key).resolve()
        skill_root = (package_root / directory).resolve()
        target = (skill_root / relative_path).resolve()
        if not target.is_relative_to(skill_root):
            raise ValueError("invalid skill resource path")
        if not target.is_file():
            raise KeyError(f"no such skill resource: {relative_path}")
        if target.stat().st_size > 256 * 1024:
            raise ValueError("skill resource exceeds 256 KiB")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("skill resource is not UTF-8 text") from exc
        return {
            "name": display_name,
            "path": f"{directory}/{relative_path}" if directory else relative_path,
            "content": content,
            "repository": item["repository"],
            "revision": item["revision"],
        }

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

    @staticmethod
    def _npx_invocation() -> list[str]:
        """Resolve npx to a spawnable command, including Windows .cmd shims."""
        executable = shutil.which("npx") or shutil.which("npx.cmd")
        if executable is None:
            raise RuntimeError("npx is not available in this deployment")
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", executable]
        return [executable]

    @staticmethod
    def _npx_skill_name(name: str) -> str:
        value = str(name or "").strip()
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or ":" in value
        ):
            raise ValueError(f"invalid skill name from npx: {name!r}")
        return value

    def _repository_for_spec(self, spec: str, source: str = "") -> str:
        """Normalize an npx source to the owner/repository key used by state."""
        for value in (source, spec):
            text = str(value or "").strip()
            text = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", text)
            text = re.sub(r"\.git$", "", text).strip("/")
            if _PACKAGE.fullmatch(text):
                return text
        digest = hashlib.sha256(str(spec).encode("utf-8")).hexdigest()[:12]
        return f"local/npx-{digest}"

    def _npx_skills(self, workdir: Path, stdout: str) -> list[dict]:
        """Find the canonical skill directories produced by npx skills add.

        The CLI writes the real files under .agents/skills and links or copies
        them into per-agent directories. Prefer that canonical tree; fall back
        to the JSON path fields or to a plain SKILL.md scan.
        """
        records: dict[str, dict] = {}
        try:
            parsed = json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            parsed = (
                [parsed]
                if parsed.get("name")
                else (parsed.get("skills") or parsed.get("installed") or [])
            )
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if name:
                    records[str(name)] = item

        found: dict[str, dict] = {}
        canonical = workdir / ".agents" / "skills"
        if canonical.is_dir():
            for directory in sorted(canonical.iterdir()):
                if not (directory / "SKILL.md").is_file():
                    continue
                name = self._npx_skill_name(directory.name)
                found[name] = {
                    "name": name,
                    "path": directory,
                    "source": str(records.get(name, {}).get("source") or ""),
                }

        for name, record in records.items():
            if name in found:
                continue
            raw_path = record.get("path")
            if not raw_path:
                continue
            directory = Path(str(raw_path))
            if (directory / "SKILL.md").is_file():
                safe_name = self._npx_skill_name(name)
                found[safe_name] = {
                    "name": safe_name,
                    "path": directory,
                    "source": str(record.get("source") or ""),
                }

        if not found:
            for skill_file in sorted(workdir.rglob("SKILL.md")):
                if ".agents" in skill_file.parts:
                    continue
                directory = skill_file.parent
                name = self._npx_skill_name(directory.name)
                found.setdefault(name, {
                    "name": name,
                    "path": directory,
                    "source": str(records.get(name, {}).get("source") or ""),
                })

        if not found:
            raise RuntimeError("npx skills add produced no SKILL.md")
        return [found[name] for name in sorted(found)]

    def _import_npx_skills(self, repository: str, skills: list[dict]) -> dict:
        """Copy canonical npx skill directories into the store package layout."""
        with _LOCK:
            state = self._state()
            previous = state["packages"].get(repository)
            with tempfile.TemporaryDirectory(dir=self.root) as temporary:
                stage = Path(temporary) / "package"
                stage.mkdir()
                if previous is not None:
                    existing = self.root / str(previous.get("directory") or "")
                    if existing.is_dir():
                        shutil.copytree(existing, stage, dirs_exist_ok=True)
                        (stage / "manifest.json").unlink(missing_ok=True)
                for skill in skills:
                    target = stage / skill["name"]
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(skill["path"], target)
                skill_paths = sorted(
                    str(path.relative_to(stage)).replace("\\", "/")
                    for path in stage.rglob("SKILL.md")
                )
                if not skill_paths:
                    raise ValueError("npx skills add produced no SKILL.md")
                digest = hashlib.sha256()
                for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                    digest.update(
                        str(path.relative_to(stage)).replace("\\", "/").encode("utf-8")
                    )
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                revision = digest.hexdigest()[:40]
                manifest = {
                    "repository": repository,
                    "revision": revision,
                    "skills": skill_paths,
                    "archive_sha256": digest.hexdigest(),
                }
                (stage / "manifest.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                key = hashlib.sha256(f"{repository}@{revision}".encode()).hexdigest()
                destination = self.root / key
                if destination.exists():
                    shutil.rmtree(destination)
                stage.replace(destination)
            state["packages"][repository] = {
                "revision": revision,
                "enabled": True,
                "directory": key,
            }
            self._save(state)
        return {"repository": repository, "revision": revision, "skills": skill_paths}

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
        """Run npx skills add and import the result into this store.

        The CLI resolves packages itself, so this is the only path that can
        install by repository slug or URL. Its canonical files live under
        .agents/skills in the working directory; run it in a temporary
        directory and copy those directories into the content-addressed
        package layout the rest of the application reads.
        """
        import subprocess

        spec = self.npx_spec(spec)
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            workdir = Path(temporary)
            try:
                completed = subprocess.run(
                    [
                        *self._npx_invocation(),
                        "--yes", "skills", "add", spec,
                        "--json", "-y", "--copy", "--skill", "*",
                    ],
                    cwd=str(workdir), capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=timeout, check=False,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("npx is not available in this deployment") from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"npx skills add timed out after {timeout}s") from exc
            except OSError as exc:
                raise RuntimeError(f"npx skills add could not start: {exc}") from exc
            output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
            if completed.returncode != 0:
                raise RuntimeError(
                    f"npx skills add failed: {output[-2000:] or completed.returncode}"
                )
            try:
                skills = self._npx_skills(workdir, completed.stdout or "")
            except RuntimeError as exc:
                raise RuntimeError(f"{exc}: {output[-1000:]}") from exc
            repository = self._repository_for_spec(spec, skills[0].get("source", ""))
            try:
                imported = self._import_npx_skills(repository, skills)
            except (OSError, shutil.Error) as exc:
                raise RuntimeError(f"could not import npx skills: {exc}") from exc
        return {
            "spec": spec,
            "output": output[-4000:],
            "installed": [
                {"name": skill["name"], "path": skill["name"]} for skill in skills
            ],
            **imported,
            "tree": self.tree(),
        }
