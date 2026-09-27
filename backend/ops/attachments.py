"""Durable, non-executable files attached to an IPC conversation."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import BinaryIO


UPLOAD_ID_RE = re.compile(r"^upload_[a-f0-9]{24}$")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_BYTES = 120_000
_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class OpsAttachmentStore:
    """Store uploaded reference material outside project workspaces.

    Files are addressed by opaque ids and never executed.  Text-like files are
    included in the next IPC prompt; binary documents remain available by
    their recorded path for a tool-assisted inspection.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def save(
        self,
        source: BinaryIO,
        *,
        filename: str,
        content_type: str = "",
    ) -> dict[str, object]:
        name = Path(filename or "attachment").name
        name = _NAME_RE.sub("_", name).strip(" .")[:160] or "attachment"
        upload_id = f"upload_{secrets.token_hex(12)}"
        target = self.root / f"{upload_id}.bin"
        total = 0
        try:
            with target.open("xb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise ValueError(f"attachment exceeds {MAX_UPLOAD_BYTES} bytes")
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        metadata = {
            "id": upload_id,
            "filename": name,
            "content_type": str(content_type or "application/octet-stream")[:160],
            "size": total,
            "path": str(target),
        }
        try:
            target.with_suffix(".json").write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {key: value for key, value in metadata.items() if key != "path"}

    def metadata(self, upload_id: str) -> dict[str, object]:
        if not UPLOAD_ID_RE.fullmatch(str(upload_id)):
            raise ValueError("invalid attachment id")
        path = self.root / f"{upload_id}.json"
        if not path.is_file():
            raise KeyError(upload_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("id") != upload_id:
            raise KeyError(upload_id)
        return value

    def prompt_context(self, upload_ids: list[str] | None) -> str:
        if not upload_ids:
            return ""
        chunks: list[str] = []
        remaining = MAX_CONTEXT_BYTES
        for upload_id in upload_ids[:10]:
            metadata = self.metadata(upload_id)
            path = Path(str(metadata["path"])).resolve()
            if self.root.resolve() not in path.parents or not path.is_file():
                raise KeyError(upload_id)
            header = f"\n\nATTACHED PLATFORM DOCUMENT: {metadata['filename']} ({metadata['size']} bytes)\n"
            if remaining <= len(header):
                break
            try:
                raw = path.read_bytes()[:remaining - len(header)]
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, OSError):
                text = (
                    f"[binary document stored at {path}; use IPC tools to inspect it]"
                )
            chunks.append(header + text)
            remaining -= len(chunks[-1])
        return "".join(chunks)
