from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class IPCLogger:
    KINDS = {
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
        self._project_filename_resolver = project_filename_resolver

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value

    def _file(self, kind: str, project_id: str | None) -> Path:
        sub = self.KINDS.get(kind, "project_logs")
        folder = self.root / sub
        folder.mkdir(parents=True, exist_ok=True)
        if project_id:
            name = self._project_filename(project_id)
        else:
            name = "global.json"
        return folder / name

    def _project_filename(self, project_id: str) -> str:
        if self._project_filename_resolver is not None:
            filename = self._project_filename_resolver(project_id)
            if filename:
                return filename if filename.endswith(".json") else f"{filename}.json"
        return f"{project_id}.json"

    def log(self, kind: str, event: str, project_id: str | None = None, **fields) -> None:
        if not self._enabled:
            return
        record = {"ts": _utcnow(), "event": event, "project_id": project_id, **fields}
        with self._lock:
            path = self._file(kind, project_id)
            entries = self._read_array(path)
            entries.append(record)
            path.write_text(
                json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    # convenience wrappers
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

    def delete_project_logs(self, project_id: str) -> None:
        with self._lock:
            for sub in self.KINDS.values():
                path = self.root / sub / self._project_filename(project_id)
                path.unlink(missing_ok=True)

    def clear_all(self) -> None:
        with self._lock:
            shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _read_array(path: Path) -> list[dict]:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
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
        entries = IPCLogger._read_array(path)
        return entries if limit is None else entries[-limit:]
