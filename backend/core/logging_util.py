from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from backend.core.redaction import redact_flag


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class IPCLogger:
    KINDS: ClassVar[dict] = {
        "project": "project_logs",
        "tool": "tool_logs",
        "llm": "llm_logs",
        "memory": "memory_logs",
    }

    def __init__(
        self,
        root: str | Path = "logs",
        enabled: bool = True,
        project_filename_resolver: Callable[[str], str | None] | None = None,
    ):
        self.root = Path(root)
        self._enabled = enabled
        self._lock = threading.Lock()
        self._file_locks: dict[Path, threading.RLock] = {}
        self._stats_cache: dict[Path, dict] = {}
        self._filename_cache: dict[str, str] = {}
        self._project_filename_resolver = project_filename_resolver

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value

    def _path_lock(self, path: Path) -> threading.RLock:
        with self._lock:
            return self._file_locks.setdefault(path, threading.RLock())

    def _file(self, kind: str, project_id: str | None) -> Path:
        sub = self.KINDS.get(kind, "project_logs")
        folder = self.root / sub
        folder.mkdir(parents=True, exist_ok=True)
        name = self._project_filename(project_id) if project_id else "global.jsonl"
        return folder / name

    def _project_filename(self, project_id: str) -> str:
        with self._lock:
            cached = self._filename_cache.get(project_id)
        if cached:
            return cached
        resolved = self._project_filename_resolver(project_id) if self._project_filename_resolver else None
        if not resolved:
            return f"{project_id}.jsonl"
        filename = resolved if resolved.endswith((".jsonl", ".json")) else f"{resolved}.jsonl"
        with self._lock:
            self._filename_cache[project_id] = filename
        return filename

    @staticmethod
    def _file_id(stat) -> str:
        return f"{stat.st_dev}:{stat.st_ino}"

    @staticmethod
    def _public_stats(stats: dict) -> dict:
        return {
            "entry_count": stats["entry_count"],
            "file_id": stats["file_id"],
            "size": stats["size"],
            "scan_offset": stats["scan_offset"],
        }

    def file_stats(self, path: str | Path) -> dict:
        path = Path(path)
        with self._path_lock(path):
            try:
                stat = path.stat()
            except FileNotFoundError:
                self._stats_cache.pop(path, None)
                return {"entry_count": 0, "file_id": "", "size": 0, "scan_offset": 0}
            identity = self._file_id(stat)
            cached = self._stats_cache.get(path)
            if (
                cached
                and cached["file_id"] == identity
                and cached["size"] == stat.st_size
                and cached["mtime_ns"] == stat.st_mtime_ns
            ):
                return self._public_stats(cached)

            if path.suffix == ".json":
                entries = self._read_array(path)
                stats = {
                    "entry_count": len(entries),
                    "file_id": identity,
                    "size": stat.st_size,
                    "scan_offset": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                self._stats_cache[path] = stats
                return self._public_stats(stats)

            offset = 0
            count = 0
            if (
                cached
                and cached["file_id"] == identity
                and stat.st_size > cached["size"]
            ):
                offset = cached["scan_offset"]
                count = cached["entry_count"]
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(stat.st_size - offset)
            complete_end = data.rfind(b"\n") + 1
            for line in data[:complete_end].splitlines():
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(entry, dict):
                    count += 1
            stats = {
                "entry_count": count,
                "file_id": identity,
                "size": stat.st_size,
                "scan_offset": offset + complete_end,
                "mtime_ns": stat.st_mtime_ns,
            }
            self._stats_cache[path] = stats
            return self._public_stats(stats)

    def count_file(self, path: str | Path) -> int:
        return self.file_stats(path)["entry_count"]

    def log(self, log_kind: str, event: str, project_id: str | None = None, **fields) -> None:
        """Append one log record.

        ``log_kind`` selects the destination stream.  Keep that name distinct
        from a record's optional ``kind`` field: IPC runtime events legitimately
        mirror their own kind (for example ``tool`` or ``status``) into the LLM
        log, and using ``kind`` for both caused ``multiple values`` failures.
        """
        if not self._enabled:
            return
        safe_fields = dict(fields)
        for key in ("flag", "candidate", "normalized_flag", "secret", "password", "token"):
            if key in safe_fields and safe_fields[key] is not None:
                safe_fields[key] = redact_flag(str(safe_fields[key]))
        record = {"ts": _utcnow(), "event": event, "project_id": project_id, **safe_fields}
        payload = json.dumps(record, ensure_ascii=False) + "\n"
        path = self._file(log_kind, project_id)
        with self._path_lock(path):
            try:
                before = path.stat()
            except FileNotFoundError:
                before = None
            with path.open("a", encoding="utf-8") as fh:
                fh.write(payload)
            cached = self._stats_cache.get(path)
            if cached is None:
                return
            after = path.stat()
            if (
                before is not None
                and cached["file_id"] == self._file_id(before)
                and cached["size"] == before.st_size
                and cached["scan_offset"] == before.st_size
            ):
                self._stats_cache[path] = {
                    **cached,
                    "entry_count": cached["entry_count"] + 1,
                    "size": after.st_size,
                    "scan_offset": after.st_size,
                    "mtime_ns": after.st_mtime_ns,
                }
            else:
                self._stats_cache.pop(path, None)

    def project(self, event: str, project_id: str, **f) -> None:
        self.log("project", event, project_id, **f)

    def tool(self, event: str, project_id: str, **f) -> None:
        self.log("tool", event, project_id, **f)

    def llm(self, event: str, project_id: str, **f) -> None:
        self.log("llm", event, project_id, **f)

    def memory(self, event: str, project_id: str | None = None, **f) -> None:
        self.log("memory", event, project_id, **f)

    def read_project_log(self, project_id: str, limit: int | None = 500) -> list[dict]:
        path = self._file("project", project_id)
        return self._tail(path, limit)

    def read_log(self, kind: str, project_id: str | None, limit: int | None = 500) -> list[dict]:
        path = self._file(kind, project_id)
        return self._tail(path, limit)

    @staticmethod
    def read_file(path: str | Path, limit: int | None = 500) -> list[dict]:
        """Read an exported IPC JSONL log without creating a live log file."""

        return IPCLogger._tail(Path(path), limit)

    def delete_project_logs(self, project_id: str) -> None:
        filename = self._project_filename(project_id)
        for sub in self.KINDS.values():
            path = self.root / sub / filename
            with self._path_lock(path):
                path.unlink(missing_ok=True)
                self._stats_cache.pop(path, None)
        with self._lock:
            self._filename_cache.pop(project_id, None)

    @staticmethod
    def _read_array(path: Path) -> list[dict]:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
            return []
        except json.JSONDecodeError:
            return IPCLogger._read_jsonl(text)

    @staticmethod
    def _read_jsonl(text: str) -> list[dict]:
        out = []
        for ln in text.splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    @staticmethod
    def _tail(path: Path, limit: int | None) -> list[dict]:
        if not path.is_file():
            return []
        if limit is None or path.suffix == ".json":
            entries = IPCLogger._read_array(path)
            return entries if limit is None else entries[-limit:]
        if limit <= 0:
            return []
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks = []
            newlines = 0
            while position and newlines <= limit:
                size = min(position, 16384)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        data = b"".join(reversed(chunks))
        if position:
            data = data.partition(b"\n")[2]
        return IPCLogger._read_jsonl(data.decode("utf-8", "replace"))[-limit:]

    def read_since(self, kind: str, project_id: str, *, after: int = 0,
                   limit: int = 500, file_id: str = "") -> dict:
        return self.read_file_since(
            self._file(kind, project_id), after=after, limit=limit, file_id=file_id
        )

    @staticmethod
    def read_file_since(path: str | Path, *, after: int = 0,
                        limit: int = 500, file_id: str = "") -> dict:
        path = Path(path)
        if not path.is_file():
            return {"entries": [], "next_offset": 0, "file_id": "", "has_more": False}
        stat = path.stat()
        identity = IPCLogger._file_id(stat)
        if (file_id and file_id != identity) or after > stat.st_size:
            after = 0
        records = []
        with path.open("rb") as handle:
            handle.seek(max(0, after))
            while len(records) < max(1, min(1000, limit)):
                position = handle.tell()
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    handle.seek(position)
                    break
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict):
                    records.append(record)
            offset = handle.tell()
        return {"entries": records, "next_offset": offset, "file_id": identity,
                "has_more": offset < path.stat().st_size}

    @staticmethod
    def jsonl_text(entries: list[dict]) -> str:
        return "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
