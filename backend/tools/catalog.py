from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.memory.memory_store import _tokenize
from backend.tools.tool_registry import ToolRegistry

CATALOG_PATH = Path(__file__).with_name("catalog.yaml")
CATALOG_DOCS_PATH = Path(__file__).with_name("catalog_docs")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOC_LINK_RE = re.compile(r"/memory/catalog/([a-z0-9][a-z0-9_-]*)/document")


@dataclass(slots=True)
class CatalogEntry:
    id: str
    kind: str
    group: str
    title: str
    summary: str
    tags: list[str] = field(default_factory=list)
    command: str = ""
    install_path: str = ""
    workflow: str = ""
    example: str = ""
    limitations: str = ""
    official: str = ""
    related: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "group": self.group,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "parents": self.parent_paths(),
            "doc_path": f"catalog_docs/{self.id}.md",
            "doc_url": f"/memory/catalog/{self.id}/document",
        }

    def parent_paths(self) -> list[str]:
        return list(dict.fromkeys([self.group, *self.parents]))


class ToolCatalog:
    """Validated read-only catalog exposed by the Web UI and Memory MCP."""

    def __init__(
        self,
        entries: list[CatalogEntry],
        group_titles: dict[str, str],
        docs_dir: Path | None = None,
    ):
        self.group_titles = group_titles
        self.docs_dir = docs_dir
        self._entries: dict[str, CatalogEntry] = {}
        for entry in entries:
            if not _ID_RE.fullmatch(entry.id):
                raise ValueError(f"invalid catalog id: {entry.id}")
            if entry.id in self._entries:
                raise ValueError(f"duplicate catalog id: {entry.id}")
            if "\n" in entry.summary or not entry.summary.strip():
                raise ValueError(f"catalog summary must be one line: {entry.id}")
            self._entries[entry.id] = entry
        for entry in entries:
            missing = [item for item in entry.related if item not in self._entries]
            if missing:
                raise ValueError(
                    f"catalog entry {entry.id} has missing related ids: {missing}"
                )
        if self.docs_dir is not None:
            self._validate_documents()

    @classmethod
    def load(
        cls,
        registry: ToolRegistry | None = None,
        path: str | Path = CATALOG_PATH,
        *,
        validate_documents: bool = True,
    ) -> "ToolCatalog":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        groups = {str(k): str(v) for k, v in (data.get("groups") or {}).items()}
        entries = [CatalogEntry(**item) for item in data.get("entries", [])]

        registry = registry or ToolRegistry(cache_db=None).load()
        existing = {entry.id for entry in entries}
        for tool in registry.all_tools():
            entry_id = "tool_" + re.sub(r"[^a-z0-9]+", "_", tool.name.lower()).strip("_")
            current = next(
                (
                    entry
                    for entry in entries
                    if entry.id == entry_id
                    or entry.title.casefold() == tool.name.casefold()
                ),
                None,
            )
            if current is not None:
                current.tags = sorted(
                    {*current.tags, tool.category, *tool.tags}
                )
                parent = f"tools/{tool.category}"
                if parent not in current.parent_paths():
                    current.parents.append(parent)
                continue
            command = tool.exec
            example = tool.exec
            if tool.exec.startswith("mcp:"):
                server_name = tool.exec.split(":", 1)[1]
                command = f"ipc-mcp-server {server_name}"
                example = f"ipc-mcp-server {server_name} --help"
            elif tool.exec == "python":
                command = "python3"
                example = "python3 -c \"import sys; print(sys.version)\""
            entries.append(
                CatalogEntry(
                    id=entry_id,
                    kind="tool",
                    group=f"tools/{tool.category}",
                    title=tool.name,
                    summary=tool.description,
                    tags=[tool.category, *tool.tags],
                    command=command,
                    install_path=tool.path or "PATH / Python site-packages",
                    workflow=tool.when_to_use,
                    example=example,
                    limitations="仅在合法授权的 CTF、靶场和研究环境中使用。",
                )
            )
            existing.add(entry_id)
        docs_dir = Path(path).with_name("catalog_docs") if validate_documents else None
        return cls(entries, groups, docs_dir)

    def _validate_documents(self) -> None:
        assert self.docs_dir is not None
        for entry_id in self._entries:
            document = self.docs_dir / f"{entry_id}.md"
            if not document.is_file():
                raise ValueError(f"missing catalog document: {document}")
            source = document.read_text(encoding="utf-8")
            for linked_id in _DOC_LINK_RE.findall(source):
                if linked_id not in self._entries:
                    raise ValueError(
                        f"catalog document {entry_id} links to unknown id: {linked_id}"
                    )

    def get(self, entry_id: str) -> CatalogEntry | None:
        return self._entries.get(entry_id)

    def entries(self) -> list[CatalogEntry]:
        return list(self._entries.values())

    def search(self, query: str, limit: int = 8) -> list[tuple[CatalogEntry, float]]:
        terms = [term for term in _tokenize(query.lower()) if len(term) >= 2]
        scored = []
        for entry in self._entries.values():
            title_terms = set(_tokenize(entry.title.lower()))
            tags = {tag.lower() for tag in entry.tags}
            body = f"{entry.summary} {entry.workflow}".lower()
            score = 0.0
            for term in terms:
                if term in title_terms or term in entry.title.lower():
                    score += 3.0
                if term in tags:
                    score += 2.5
                if term in body:
                    score += 1.0
            if score:
                scored.append((entry, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].title.casefold()))
        return scored[: max(1, min(limit, 100))]

    def tree(self) -> list[dict[str, Any]]:
        root: dict[str, Any] = {"children": {}}
        for entry in sorted(
            self._entries.values(),
            key=lambda item: (item.group, item.title.casefold()),
        ):
            for parent in entry.parent_paths():
                node = root
                path = ""
                for segment in parent.split("/"):
                    path = f"{path}/{segment}".strip("/")
                    node = node["children"].setdefault(
                        segment,
                        {
                            "id": f"group:{path}",
                            "path": path,
                            "title": self.group_titles.get(path, segment),
                            "kind": "group",
                            "children": {},
                            "entries": [],
                        },
                    )
                node["entries"].append(entry.to_dict())

        def serialise(node: dict[str, Any]) -> list[dict[str, Any]]:
            out = []
            for child in node["children"].values():
                out.append(
                    {
                        "id": child["id"],
                        "path": child["path"],
                        "title": child["title"],
                        "kind": "group",
                        "children": serialise(child),
                        "entries": child["entries"],
                    }
                )
            return out

        return serialise(root)

    def browse(self, path: str | None = None) -> dict[str, Any]:
        if not path:
            return {"path": "", "children": self.tree(), "entries": []}
        wanted = path.strip("/")
        entries = [
            entry.to_dict()
            for entry in self._entries.values()
            if entry.group == wanted
        ]
        children = [
            node
            for node in self._walk_groups(self.tree())
            if node["path"].rsplit("/", 1)[0] == wanted
        ]
        if not entries and not children and wanted not in self.group_titles:
            return {"error": f"unknown catalog path: {path}"}
        return {"path": wanted, "children": children, "entries": entries}

    def _walk_groups(self, nodes: list[dict[str, Any]]):
        for node in nodes:
            yield {**node, "children": []}
            yield from self._walk_groups(node["children"])

    def document(self, entry_id: str) -> str:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        if self.docs_dir is None:
            raise RuntimeError("catalog has no document directory")
        document = (self.docs_dir / f"{entry.id}.md").resolve()
        if document.parent != self.docs_dir.resolve():
            raise KeyError(entry_id)
        return document.read_text(encoding="utf-8")

    def render_document(self, entry_id: str) -> str:
        """Render a catalog document for maintainers regenerating package data."""
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        command = entry.command or "参见下方工作流与对应运行时文档。"
        example = entry.example or command
        workflow = entry.workflow or f"先确认输入与目标，再使用 {entry.title} 完成针对性分析。"
        limitations = entry.limitations or "版本和参数可能随镜像升级变化，执行前先查看 `--help`。"
        first_command = command.splitlines()[0]
        if "version" in first_command.casefold():
            version_command = first_command
        elif first_command.startswith("ipc-mcp-server"):
            version_command = "ipc-mcp-server --help"
        else:
            version_command = f"{first_command} --version"
        related = [
            self._entries[item] for item in entry.related if item in self._entries
        ]
        lines = [
            f"# {entry.title}",
            "",
            entry.summary,
            "",
            "## 用途与适用场景",
            "",
            workflow,
            "",
            "## 版本检查",
            "",
            "```bash",
            version_command,
            "```",
            "",
            "## 命令、导入与镜像路径",
            "",
            f"- 常用入口：`{command}`",
            f"- 镜像路径：`{entry.install_path or 'PATH / 对应语言的包目录'}`",
            "",
            "## 常用工作流",
            "",
            "1. 在 Member 工作目录中确认附件、目标地址和授权范围。",
            f"2. 按题目类型使用 `{first_command}` 进行最小化探测。",
            "3. 保存原始输出，再根据结果逐步增加参数，避免一开始执行破坏性操作。",
            "",
            "## 可执行示例",
            "",
            "```bash",
            example,
            "```",
            "",
            "## 输出解释",
            "",
            "重点检查退出码、错误输出、命中项、地址/偏移和生成文件；将可复现结论记录到项目事实与 WP。",
            "",
            "## 常见错误与限制",
            "",
            limitations,
            "",
            "## 关联条目",
            "",
        ]
        if related:
            lines.extend(
                f"- [{item.title}](/memory/catalog/{item.id}/document)"
                for item in related
            )
        else:
            lines.append("- 可通过 Memory 工具目录返回同级目录查看相关能力。")
        lines.extend(["", "## 官方参考", ""])
        if entry.official:
            lines.append(f"- [{entry.official}]({entry.official})")
        else:
            lines.append("- 使用镜像内 `--help`、语言内置帮助或工具上游仓库作为版本对应参考。")
        lines.append("")
        return "\n".join(lines)

    def html_document(self, entry_id: str) -> str:
        # Catalog documents are trusted package data. Keep a dependency-free
        # renderer for the small supported Markdown subset used by the UI.
        source = self.document(entry_id)
        blocks = []
        in_code = False
        code_lines: list[str] = []
        list_open = False
        for raw in source.splitlines():
            if raw.startswith("```"):
                if in_code:
                    blocks.append(
                        "<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>"
                    )
                    code_lines = []
                    in_code = False
                else:
                    in_code = True
                continue
            if in_code:
                code_lines.append(raw)
                continue
            if raw.startswith("# "):
                blocks.append(f"<h1>{html.escape(raw[2:])}</h1>")
            elif raw.startswith("## "):
                if list_open:
                    blocks.append("</ul>")
                    list_open = False
                blocks.append(f"<h2>{html.escape(raw[3:])}</h2>")
            elif raw.startswith("- "):
                if not list_open:
                    blocks.append("<ul>")
                    list_open = True
                blocks.append(f"<li>{self._inline_html(raw[2:])}</li>")
            elif re.match(r"^\d+\. ", raw):
                blocks.append(f"<p>{self._inline_html(raw)}</p>")
            elif raw.strip():
                if list_open:
                    blocks.append("</ul>")
                    list_open = False
                blocks.append(f"<p>{self._inline_html(raw)}</p>")
        if list_open:
            blocks.append("</ul>")
        return "\n".join(blocks)

    @staticmethod
    def _inline_html(text: str) -> str:
        escaped = html.escape(text)
        escaped = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            r'<a href="\2" target="_blank" rel="noopener">\1</a>',
            escaped,
        )
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
