from __future__ import annotations

import json
import threading
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

    def __init__(self, root: str | Path = "logs", enabled: bool = True):
        self.root = Path(root)
        self._enabled = enabled
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = value

    def _file(self, kind: str, project_id: str | None) -> Path:
        sub = self.KINDS.get(kind, "project_logs")
        folder = self.root / sub
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{project_id}.jsonl" if project_id else "global.jsonl"
        return folder / name

    def log(self, kind: str, event: str, project_id: str | None = None, **fields) -> None:
        if not self._enabled:
            return
        record = {"ts": _utcnow(), "event": event, "project_id": project_id, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self._file(kind, project_id).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # convenience wrappers
    def project(self, event: str, project_id: str, **f) -> None:
        self.log("project", event, project_id, **f)

    def tool(self, event: str, project_id: str, **f) -> None:
        self.log("tool", event, project_id, **f)

    def llm(self, event: str, project_id: str, **f) -> None:
        self.log("llm", event, project_id, **f)

    def memory(self, event: str, project_id: str | None = None, **f) -> None:
        self.log("memory", event, project_id, **f)

    def read_project_log(self, project_id: str, limit: int = 500) -> list[dict]:
        path = self._file("project", project_id)
        return self._tail(path, limit)

    def read_log(self, kind: str, project_id: str | None, limit: int = 500) -> list[dict]:
        path = self._file(kind, project_id)
        return self._tail(path, limit)

    @staticmethod
    def _tail(path: Path, limit: int) -> list[dict]:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out
