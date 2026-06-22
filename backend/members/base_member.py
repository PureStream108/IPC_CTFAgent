from __future__ import annotations

import json
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from backend.blackboard import edge_store, graph_store, node_store
from backend.core.difficulty import (
    DIFFICULTY_RANK,
    detect_attack_surfaces,
    detect_exploit_classes,
    max_difficulty,
    normalize_difficulty,
)
from backend.core.logging_util import IPCLogger
from backend.mcp.base import MCPRegistry
from backend.members.adapters import BaseAdapter, MemberAction
from backend.memory.memory_search import search as mem_search
from backend.memory.memory_store import MemoryStore
from backend.sandbox.sandbox import Sandbox
from backend.tools.tool_mcp import build_category_tools_mcp
from backend.tools.tool_inventory import member_tool_inventory, member_tool_inventory_path
from backend.tools.tool_registry import LANGUAGES, PUBLIC_MCPS, ToolRegistry

_LOCAL_WEBUI_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):(\d{2,5})\b")
_PORT_FLAG_RE = re.compile(r"(?:^|\s)(?:--port|-p)\s+(\d{2,5})(?:\s|$)")
_WEBUI_HINT_RE = re.compile(r"\b(webui|gradio|streamlit|jupyter|flask|uvicorn)\b", re.IGNORECASE)
_COMMAND_NOT_FOUND_RE = re.compile(
    r"(?m)(?:^|: )([A-Za-z0-9_.+-]+): (?:command not found|not found)\b"
)
_WINDOWS_COMMAND_NOT_FOUND_RE = re.compile(r"'([^']+)' is not recognized", re.IGNORECASE)
_CLI_PROBE_TOOLS = (
    "rg", "grep", "find", "git", "curl", "wget", "jq", "python3", "python",
    "file", "strings", "xxd", "unzip", "zip", "openssl", "nmap", "gdb",
    "sqlmap", "sage", "node", "npm", "php", "ruby",
)


@dataclass
class MemberDeps:
    db: Any
    logger: IPCLogger
    sandbox: Sandbox
    mcps: MCPRegistry
    registry: ToolRegistry
    memory: MemoryStore
    eval_interval: int = 7
    max_steps: int = 60
    max_actions_per_task: int = 4
    on_report: Callable[[str, Any], None] | None = None   # (project_id, Report)
    on_flag: Callable[[str], None] | None = None           # (project_id)
    expected_flag: str | None = None


@dataclass
class SolveResult:
    status: str          # concluded | flag | done | stalled | failed | stopped
    steps: int
    fact_id: str | None = None
    flag: str | None = None


@dataclass
class DispatchResult:
    result: SolveResult | None = None
    graph_action: str | None = None
    invalid_action: bool = False
    invalid_knowledge: list[str] | None = None


class BaseMember:
    role_blurb = "a versatile CTF solver"

    def __init__(self, name: str, adapter: BaseAdapter, deps: MemberDeps):
        self.name = name
        self.adapter = adapter
        self.deps = deps
        self._stop = threading.Event()
        self.observations: list[str] = []
        self._recent_action_sigs: deque[str] = deque(maxlen=12)
        self._pending_bumps: list[str] = []
        self._tool_availability: dict[str, bool] | None = None
        self._missing_tool_counts: dict[str, int] = {}
        self._state_lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def solve(self, project_id: str, intent_id: str, category: str, is_initial: bool = False) -> SolveResult:
        d = self.deps
        d.logger.project("member_start", project_id, member=self.name, intent=intent_id, initial=is_initial)
        self._claim(project_id, intent_id)
        self._seed_tool_inventory(project_id)
        self._prime_tool_context(project_id, intent_id, category)
        step = 0
        task_budget = max(1, min(d.max_steps, d.max_actions_per_task))
        graph_actions: list[str] = []
        branch_intents = 0
        invalid_actions = 0
        while step < task_budget and not self._stop.is_set():
            step += 1
            evaluate_now = step % d.eval_interval == 0
            context = self._build_context(project_id, intent_id, category, step, is_initial, evaluate_now)
            try:
                action = self.adapter.decide(context)
            except Exception as exc:
                d.logger.project("member_error", project_id, member=self.name, error=str(exc))
                self._release(project_id, intent_id)
                return SolveResult(status="failed", steps=step)
            d.logger.llm("decide", project_id, member=self.name, step=step,
                         thought=action.thought, action=action.kind)
            self._heartbeat(project_id, intent_id)
            loop_status = self._record_action_signature(action)
            if loop_status == "warn":
                self._observe(
                    "[stuckness] You have repeated the same action signature several times. "
                    "Stop replaying it and switch to a distinct exploit class, tool, or evidence source."
                )
                d.logger.project(
                    "member_loop_warning",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    steps=step,
                )
            if loop_status == "break":
                self._submit_stall_report(
                    project_id,
                    intent_id,
                    category,
                    step,
                    difficulty_hint="medium",
                    extra_knowledge=["action_signature_repeat"],
                )
                self._release(project_id, intent_id)
                d.logger.project("member_loop_detected", project_id, member=self.name, intent=intent_id, steps=step)
                return SolveResult(status="stalled", steps=step)

            dispatched = self._dispatch(
                project_id,
                intent_id,
                category,
                action,
                step,
                allow_intent=branch_intents < 1,
            )
            if dispatched.graph_action is not None:
                graph_actions.append(dispatched.graph_action)
                if dispatched.graph_action == "intent":
                    branch_intents += 1
            if dispatched.invalid_action:
                invalid_actions += 1
                if invalid_actions >= 2:
                    invalid_knowledge = dispatched.invalid_knowledge or []
                    self._submit_stall_report(
                        project_id,
                        intent_id,
                        category,
                        step,
                        difficulty_hint="medium",
                        extra_knowledge=["invalid_action_contract", *invalid_knowledge],
                    )
                    self._release(project_id, intent_id)
                    d.logger.project(
                        "member_invalid_action_limit",
                        project_id,
                        member=self.name,
                        intent=intent_id,
                        steps=step,
                    )
                    return SolveResult(status="stalled", steps=step)
            else:
                invalid_actions = 0
            if dispatched.result is not None:
                return dispatched.result
        if self._stop.is_set():
            self._release(project_id, intent_id)
            d.logger.project("member_stopped", project_id, member=self.name, intent=intent_id, steps=step)
            return SolveResult(status="stopped", steps=step)

        if not graph_actions:
            self._submit_stall_report(project_id, intent_id, category, step)
        self._release(project_id, intent_id)
        status = "done" if graph_actions else "stalled"
        d.logger.project(
            "member_task_finished",
            project_id,
            member=self.name,
            intent=intent_id,
            status=status,
            steps=step,
            graph_actions=graph_actions,
        )
        return SolveResult(status=status, steps=step)

    def _dispatch(
        self,
        project_id,
        intent_id,
        category,
        action: MemberAction,
        step,
        *,
        allow_intent: bool,
    ) -> DispatchResult:
        d = self.deps
        kind = action.kind
        if kind == "bash":
            cmd = self._string_arg(action.args.get("command", ""))
            if not cmd.strip():
                d.logger.project(
                    "invalid_bash_action",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    keys=sorted(action.args),
                )
                self._observe("[invalid bash action omitted: missing non-empty `command`]")
                return DispatchResult(invalid_action=True)
            res = d.sandbox.exec(cmd, timeout=60)
            self._observe(f"$ {cmd}\n{res.stdout}\n{res.stderr}".strip())
            self._observe_webui_links(project_id, cmd, res.stdout, res.stderr)
            d.logger.tool(
                "bash",
                project_id,
                member=self.name,
                command=cmd,
                exit_code=res.exit_code,
                stdout=res.stdout[:4000],
                stderr=res.stderr[:4000],
            )
            missing_command = self._missing_command_from_result(cmd, res.stderr)
            if missing_command:
                knowledge = self._record_unavailable_cli(project_id, intent_id, missing_command)
                return DispatchResult(invalid_action=True, invalid_knowledge=knowledge)
            return DispatchResult()
        if kind == "tool":
            server = self._string_arg(action.args.get("server", ""))
            tool = self._string_arg(action.args.get("tool", ""))
            args = action.args.get("args", {})
            if not isinstance(args, dict):
                args = {}
            if not str(server).strip() or not str(tool).strip():
                d.logger.project(
                    "invalid_tool_action",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    keys=sorted(action.args),
                )
                self._observe("[invalid tool action omitted: missing `server` or `tool`]")
                return DispatchResult(invalid_action=True)
            try:
                if server == "tools":
                    out = build_category_tools_mcp(d.registry, category).call(tool, **args)
                else:
                    out = d.mcps.call(server, tool, **args)
            except Exception as exc:
                out = {"error": str(exc)}
            self._observe(f"[mcp:{server}.{tool}] {out}")
            d.logger.tool("mcp_call", project_id, member=self.name, server=server, tool=tool)
            return DispatchResult()
        if kind == "memory":
            query = self._string_arg(action.args.get("query", ""))
            hits = mem_search(d.memory, query, limit=5)
            self._observe(f"[memory:{query}] " + "; ".join(f"{m.title}" for m, _ in hits))
            d.logger.memory("search", project_id, member=self.name, query=query, hits=len(hits))
            return DispatchResult()
        if kind == "tool_search":
            query = self._string_arg(action.args.get("query", ""))
            tools = d.registry.search(query)
            self._observe(f"[tool_search:{query}] " + ", ".join(t.name for t in tools))
            d.logger.tool("tool_search", project_id, member=self.name, query=query)
            return DispatchResult()
        if kind == "report":
            report = self._submit_report(project_id, intent_id, action)
            return DispatchResult(graph_action="report" if report is not None else None)
        if kind == "intent":
            if not allow_intent:
                d.logger.project(
                    "intent_budget_exhausted",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    description=action.args.get("description", "explore"),
                )
                return DispatchResult()
            created = self._declare_intent(project_id, intent_id, action)
            return DispatchResult(graph_action="intent" if created is not None else None)
        if kind == "conclude":
            return DispatchResult(result=self._conclude(project_id, intent_id, action), graph_action="conclude")
        if kind == "flag":
            return DispatchResult(result=self._raise_flag(project_id, intent_id, action, step), graph_action="flag")
        if kind == "done":
            self._release(project_id, intent_id)
            d.logger.project("member_done", project_id, member=self.name, reason=action.args.get("reason"))
            return DispatchResult(result=SolveResult(status="done", steps=step))
        return DispatchResult()

    def _claim(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            edge_store.claim_intent(conn, project_id, intent_id, self.name)

    def _heartbeat(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is not None and row["to_fact_id"] is None:
                edge_store.claim_intent(conn, project_id, intent_id, self.name)

    def _release(self, project_id, intent_id):
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is not None and row["to_fact_id"] is None and row["worker"] == self.name:
                edge_store.release_intent(conn, project_id, intent_id)

    def _seed_tool_inventory(self, project_id: str) -> None:
        try:
            self.deps.sandbox.write_file(
                "tools.txt",
                member_tool_inventory().rstrip() + "\n\n" + self._runtime_tool_inventory_note(),
            )
        except Exception as exc:
            self.deps.logger.project(
                "member_tool_inventory_seed_failed",
                project_id,
                member=self.name,
                error=str(exc),
            )

    def _prime_tool_context(self, project_id: str, intent_id: str, category: str) -> None:
        try:
            with self.deps.db.connect() as conn:
                row = edge_store.get_intent(conn, project_id, intent_id)
                goal = next(
                    (fact.description for fact in node_store.list_facts(conn, project_id) if fact.id == "goal"),
                    "",
                )
            intent_desc = row["description"] if row is not None else ""
            query = " ".join(part for part in (category, intent_desc, goal) if part).strip()
            if not query:
                return
            tools = self.deps.registry.search(query)[:5]
        except Exception as exc:
            self.deps.logger.project(
                "member_tool_context_prime_failed",
                project_id,
                member=self.name,
                intent=intent_id,
                error=str(exc),
            )
            return
        summary = ", ".join(
            f"{tool.name}({tool.category})" for tool in tools
        ) or "no matching registered tools"
        self._observe(f"[tool_search:auto:{query[:120]}] {summary}")
        self.deps.logger.tool(
            "tool_search",
            project_id,
            member=self.name,
            query=query,
            hits=[tool.name for tool in tools],
            automatic=True,
        )

    def _probe_cli_tools(self) -> dict[str, bool]:
        if self._tool_availability is not None:
            return self._tool_availability
        names_json = json.dumps(list(_CLI_PROBE_TOOLS))
        code = (
            "import json, shutil; "
            f"names=json.loads('{names_json}'); "
            "print(json.dumps({n: bool(shutil.which(n)) for n in names}, sort_keys=True))"
        )
        command = f'python3 -c "{code}" || python -c "{code}"'
        availability: dict[str, bool] = {}
        try:
            res = self.deps.sandbox.exec(command, timeout=10)
            for line in reversed(res.stdout.splitlines()):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    availability = {str(k): bool(v) for k, v in parsed.items()}
                    break
        except Exception:
            availability = {}
        self._tool_availability = availability
        return availability

    def _runtime_tool_inventory_note(self) -> str:
        availability = self._probe_cli_tools()
        if not availability:
            return (
                "Runtime tool availability\n"
                "-------------------------\n"
                "- probe: unavailable; trust the static inventory only after checking commands with `command -v` or `which`."
            )
        available = ", ".join(name for name, ok in availability.items() if ok) or "none"
        missing = ", ".join(name for name, ok in availability.items() if not ok) or "none"
        return (
            "Runtime tool availability\n"
            "-------------------------\n"
            f"- available now: {available}\n"
            f"- missing now: {missing}\n"
            "- If a command is missing, switch to an available fallback or call tool_search/MCP instead of retrying it."
        )

    def _record_unavailable_cli(self, project_id: str, intent_id: str, command_name: str) -> list[str]:
        command_name = command_name.strip()
        if not command_name:
            return []
        availability = self._probe_cli_tools()
        availability[command_name] = False
        self._tool_availability = availability
        count = self._missing_tool_counts.get(command_name, 0) + 1
        self._missing_tool_counts[command_name] = count
        self._observe(
            f"[tool unavailable:{command_name}] This command is missing in the sandbox. "
            "Use `cat tools.txt` to choose an installed fallback, or call tool_search/MCP."
        )
        self.deps.logger.project(
            "sandbox_tool_unavailable",
            project_id,
            member=self.name,
            intent=intent_id,
            command=command_name,
            count=count,
        )
        return [f"unavailable_cli_tool:{command_name}"]

    def _missing_command_from_result(self, command: str, stderr: str) -> str | None:
        text = stderr or ""
        for match in _WINDOWS_COMMAND_NOT_FOUND_RE.finditer(text):
            return match.group(1)
        for match in _COMMAND_NOT_FOUND_RE.finditer(text):
            candidate = match.group(1)
            if candidate not in {"line", "bash", "sh"}:
                return candidate
        if "not found" in text.lower() or "command not found" in text.lower():
            first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
            return first or None
        return None

    def _submit_report(self, project_id, intent_id, action: MemberAction):
        d = self.deps
        a = dict(action.args)
        with d.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            node_id = row["to_fact_id"] if row else None
            if node_id is None and row is not None:
                sources = self._intent_source_ids(conn, project_id, intent_id)
                node_id = sources[-1] if sources else None
            progress = self._string_arg(a.get("progress", ""))
            steps = self._list_arg(a.get("steps", []))
            directions = self._list_arg(a.get("directions", []))
            knowledge = self._list_arg(a.get("knowledge", []))
            intent_tag = f"intent:{intent_id}"
            if intent_tag not in knowledge:
                knowledge.append(intent_tag)
            difficulty, evidence = self._calibrate_difficulty(
                conn,
                project_id,
                intent_id,
                node_id,
                progress,
                self._string_arg(a.get("difficulty", "low")) or "low",
                steps,
                directions,
                knowledge,
            )
            for item in evidence:
                tag = f"evidence:{item}"
                if tag not in knowledge:
                    knowledge.append(tag)
            if self._should_suppress_report(conn, project_id, intent_id, node_id, difficulty, evidence):
                d.logger.project(
                    "difficulty_report_suppressed",
                    project_id,
                    member=self.name,
                    intent=intent_id,
                    difficulty=difficulty,
                    reason="unchanged_difficulty",
                )
                return None
            report = graph_store.create_report(
                conn, project_id, self.name, progress, difficulty,
                node_id, steps, directions, knowledge,
            )
            graph_store.add_link(conn, project_id, self.name, "diamond", "report")
        d.logger.project(
            "difficulty_report",
            project_id,
            member=self.name,
            difficulty=report.difficulty,
            evidence=evidence,
        )
        if d.on_report is not None:
            d.on_report(project_id, report)
        return report

    def _list_arg(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    def _calibrate_difficulty(
        self,
        conn,
        project_id: str,
        intent_id: str,
        node_id: str | None,
        progress: str,
        requested: str,
        steps: list[str],
        directions: list[str],
        knowledge: list[str],
    ) -> tuple[str, list[str]]:
        level = normalize_difficulty(requested)
        evidence: list[str] = []
        reports = graph_store.list_reports(conn, project_id)
        intent_tag = f"intent:{intent_id}"
        scoped = [
            report for report in reports
            if intent_tag in report.knowledge or (node_id is not None and report.node_id == node_id)
        ]

        if "short_task_stall" in knowledge or "no_new_fact" in knowledge:
            prior_no_fact = sum(
                1 for report in scoped
                if "short_task_stall" in report.knowledge or "no_new_fact" in report.knowledge
            )
            no_fact_count = prior_no_fact + 1
            if no_fact_count >= 3:
                level = max_difficulty(level, "high")
                evidence.append(f"no_new_fact_short_tasks:{no_fact_count}")
            elif no_fact_count >= 2:
                level = max_difficulty(level, "medium")
                evidence.append(f"no_new_fact_short_tasks:{no_fact_count}")

        if "action_signature_repeat" in knowledge:
            prior_repeats = sum(1 for report in scoped if "action_signature_repeat" in report.knowledge)
            level = max_difficulty(level, "high" if prior_repeats else "medium")
            evidence.append("action_signature_repeat")

        # Calibrate from observed evidence, not speculative next-step phrasing.
        texts = [progress, *steps, *knowledge]
        for report in scoped[-5:]:
            texts.extend([report.progress, *report.steps, *report.knowledge])
        exploit_classes = detect_exploit_classes(texts)
        if len(exploit_classes) >= 4:
            level = max_difficulty(level, "ex")
            evidence.append("distinct_exploit_classes:4+")
        elif len(exploit_classes) >= 3:
            level = max_difficulty(level, "high")
            evidence.append("distinct_exploit_classes:3")
        elif len(exploit_classes) >= 2:
            level = max_difficulty(level, "medium")
            evidence.append("distinct_exploit_classes:2")

        surface_texts = list(texts)
        surface_texts.extend(f.description for f in node_store.list_facts(conn, project_id))
        surface_texts.extend(h.content for h in graph_store.list_hints(conn, project_id))
        surface_texts.extend(a.filename for a in graph_store.list_attachments(conn, project_id))
        surfaces = detect_attack_surfaces(surface_texts)
        if len(surfaces) >= 4:
            level = max_difficulty(level, "ex")
            evidence.append("credible_attack_surfaces:4+")
        elif len(surfaces) >= 3:
            level = max_difficulty(level, "high")
            evidence.append("credible_attack_surfaces:3")
        elif len(surfaces) >= 2:
            level = max_difficulty(level, "medium")
            evidence.append("credible_attack_surfaces:2")

        if (
            DIFFICULTY_RANK[level] >= DIFFICULTY_RANK["high"]
            and len(exploit_classes) >= 2
            and len(surfaces) >= 2
            and any(item.startswith("no_new_fact_short_tasks:") for item in evidence)
        ):
            level = max_difficulty(level, "ex")
            evidence.append("combined_stuckness")

        return level, evidence

    def _should_suppress_report(
        self,
        conn,
        project_id: str,
        intent_id: str,
        node_id: str | None,
        difficulty: str,
        evidence: list[str],
    ) -> bool:
        intent_tag = f"intent:{intent_id}"
        reports = [
            report for report in graph_store.list_reports(conn, project_id)
            if report.member == self.name
            and (intent_tag in report.knowledge or (node_id is not None and report.node_id == node_id))
        ]
        if not reports:
            return False
        latest = reports[-1]
        if normalize_difficulty(latest.difficulty) != normalize_difficulty(difficulty):
            return False
        latest_evidence = {
            item.removeprefix("evidence:")
            for item in latest.knowledge
            if item.startswith("evidence:")
        }
        new_evidence = [item for item in evidence if item not in latest_evidence]
        return not new_evidence

    def _declare_intent(self, project_id, current_intent_id, action: MemberAction):
        a = action.args
        requested_from = a.get("from")
        description = self._string_arg(a.get("description", "explore")) or "explore"
        with self.deps.db.connect() as conn:
            if requested_from:
                from_ids = self._list_arg(requested_from)
                for fid in from_ids:
                    if not node_store.fact_exists(conn, project_id, fid):
                        from_ids = ["origin"]
                        break
            else:
                from_ids = self._default_intent_sources(conn, project_id, current_intent_id)
            existing = edge_store.find_similar_open_intent(conn, project_id, from_ids, description)
            if existing is not None:
                self.deps.logger.project(
                    "intent_deduped",
                    project_id,
                    member=self.name,
                    existing_intent=existing.id,
                    from_ids=from_ids,
                    description=description,
                )
                return None
            intent = edge_store.create_intent(conn, project_id, from_ids, description, self.name)
            graph_store.add_link(conn, project_id, self.name, f"intent:{intent.id}", "explore")
            self.deps.logger.project(
                "intent_declared",
                project_id,
                member=self.name,
                intent=intent.id,
                from_ids=from_ids,
                description=description,
            )
            return intent

    def _intent_source_ids(self, conn, project_id, intent_id) -> list[str]:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (intent_id, project_id),
        ).fetchall()
        return [row["fact_id"] for row in rows]

    def _default_intent_sources(self, conn, project_id, intent_id) -> list[str]:
        current_sources = [
            fact_id
            for fact_id in self._intent_source_ids(conn, project_id, intent_id)
            if node_store.fact_exists(conn, project_id, fact_id)
        ]
        non_root_sources = [fact_id for fact_id in current_sources if fact_id not in ("origin", "goal")]
        if non_root_sources:
            return non_root_sources

        facts = [fact.id for fact in node_store.list_facts(conn, project_id) if fact.id not in ("origin", "goal")]
        if facts:
            return [facts[-1]]
        return current_sources or ["origin"]

    def _submit_stall_report(
        self,
        project_id,
        intent_id,
        category,
        step,
        *,
        difficulty_hint: str = "low",
        extra_knowledge: list[str] | None = None,
    ) -> None:
        recent = self.observations[-4:]
        progress = "Short exploration ended without a confirmed result."
        if recent:
            progress = "Short exploration observations:\n" + "\n\n".join(recent)
        knowledge = [category, "short_task_stall", "no_new_fact", *(extra_knowledge or [])]
        action = MemberAction(
            kind="report",
            thought="short task budget exhausted; sharing observations for follow-up",
            args={
                "progress": progress[:1800],
                "difficulty": difficulty_hint,
                "steps": recent or [f"Used {step} short-task actions on {category} intent."],
                "directions": [
                    "Try a different concrete approach for this intent.",
                    "Use sibling findings and avoid repeating the same command sequence.",
                ],
                "knowledge": knowledge,
            },
        )
        self._submit_report(project_id, intent_id, action)
        self.deps.logger.project(
            "member_stalled",
            project_id,
            member=self.name,
            intent=intent_id,
            steps=step,
        )

    def _record_action_signature(self, action: MemberAction) -> str | None:
        if action.kind not in {"bash", "tool", "tool_search", "memory"}:
            return None
        sig = action.kind + ":" + json.dumps(action.args, sort_keys=True, ensure_ascii=False)
        self._recent_action_sigs.append(sig)
        count = sum(1 for item in self._recent_action_sigs if item == sig)
        if count >= 5:
            return "break"
        if count >= 3:
            return "warn"
        return None

    def _reset_action_signatures(self) -> None:
        self._recent_action_sigs.clear()

    def bump(self, insights: str) -> None:
        text = insights.strip()
        if not text:
            return
        with self._state_lock:
            self._pending_bumps.append(text[:1800])
            self._pending_bumps = self._pending_bumps[-5:]
        self._reset_action_signatures()

    def _consume_bumps(self) -> list[str]:
        with self._state_lock:
            bumps = list(self._pending_bumps)
            self._pending_bumps.clear()
        return bumps

    def _conclude(self, project_id, intent_id, action: MemberAction) -> SolveResult:
        desc = self._string_arg(action.args.get("description", "confirmed result")) or "confirmed result"
        with self.deps.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            if row is None or row["to_fact_id"] is not None:
                return SolveResult(status="done", steps=0)
            fact = node_store.create_fact(conn, project_id, desc)
            edge_store.conclude_intent(conn, project_id, intent_id, self.name, fact.id)
            graph_store.add_link(conn, project_id, self.name, f"fact:{fact.id}", "explore")
            graph_store.touch_project(conn, project_id)
        self.deps.logger.project("intent_concluded", project_id, member=self.name, intent=intent_id, fact=fact.id)
        return SolveResult(status="concluded", steps=0, fact_id=fact.id)

    def _raise_flag(self, project_id, intent_id, action: MemberAction, step) -> SolveResult:
        d = self.deps
        flag = self._string_arg(action.args.get("flag", ""))
        desc = self._string_arg(action.args.get("description", "flag captured")) or "flag captured"
        with d.db.connect() as conn:
            row = edge_store.get_intent(conn, project_id, intent_id)
            # ensure the assigned intent is concluded into a fact first
            if row is not None and row["to_fact_id"] is None:
                fact = node_store.create_fact(conn, project_id, desc)
                edge_store.conclude_intent(conn, project_id, intent_id, self.name, fact.id)
                graph_store.add_link(conn, project_id, self.name, f"fact:{fact.id}", "explore")
                from_fact = fact.id
            else:
                from_fact = row["to_fact_id"] if row else "origin"
            # completion edge -> goal
            comp = edge_store.create_intent(conn, project_id, [from_fact], desc, self.name, worker=self.name)
            conn.execute(
                "UPDATE intents SET to_fact_id='goal', concluded_at=? WHERE id=? AND project_id=?",
                (comp.created_at, comp.id, project_id),
            )
            graph_store.set_flag(conn, project_id, flag)
            graph_store.set_status(conn, project_id, "flag_found")
            graph_store.add_link(conn, project_id, f"fact:{from_fact}", "flag", "flag")
        d.logger.project("flag_found", project_id, member=self.name, flag=flag)
        if d.on_flag is not None:
            d.on_flag(project_id)
        return SolveResult(status="flag", steps=step, flag=flag, fact_id=from_fact)

    # ---- context ----

    def _build_context(self, project_id, intent_id, category, step, is_initial, evaluate_now) -> dict:
        d = self.deps
        with d.db.connect() as conn:
            detail = graph_store.project_detail(conn, project_id)
        if detail is None:
            raise RuntimeError(f"project {project_id} not found")
        assigned = next((i for i in detail.intents if i.id == intent_id), None)
        exposed = [t.to_dict() for t in d.registry.exposed_for(category)]
        cli_availability = self._probe_cli_tools()
        reports = detail.reports[-8:]
        sibling_insights = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
                "knowledge": r.knowledge,
            }
            for r in reports
            if r.member != self.name
        ]
        pending_bumps = self._consume_bumps()
        previous_attempts = [
            {
                "member": r.member,
                "difficulty": r.difficulty,
                "progress": r.progress,
                "directions": r.directions,
            }
            for r in reports
            if r.member == self.name or (assigned and r.node_id in (None, assigned.to))
        ]
        attachments = [
            {"id": a.id, "filename": a.filename, "path": a.path, "created_at": a.created_at}
            for a in detail.attachments
        ]
        attachment_true = bool(attachments)
        attachment_path = getattr(d.sandbox, "visible_attachment_path", None)
        if callable(attachment_path):
            for attachment in attachments:
                attachment["path"] = attachment_path(attachment["filename"], attachment["path"])
        return {
            "role": self.name,
            "role_blurb": self.role_blurb,
            "category": category,
            "step": step,
            "max_steps": min(d.max_steps, d.max_actions_per_task),
            "short_task": True,
            "task_contract": (
                "This is a short exploration task. Produce one clear result quickly: "
                "flag, conclude, a useful new intent, or a difficulty report with concrete next directions. "
                "Evaluate difficulty every eval_interval steps, but report only when the assessed level changes "
                "or new evidence justifies escalation. Do not repeat previous attempts."
            ),
            "sandbox_backend": getattr(d.sandbox, "__class__", type(d.sandbox)).__name__,
            "runtime_notes": [
                "If sandbox_backend is LocalSandbox, use host shell-compatible commands only.",
                "If sandbox_backend is DockerSandbox, use Linux commands inside the container.",
                "Flag search priority for this round: try /flag first, then environment variables, then other methods.",
                "If attachment_true is true, inspect the listed attachments before blind target probing.",
            ],
            "evaluate_now": evaluate_now,
            "eval_interval": d.eval_interval,
            "is_initial": is_initial,
            "expected_flag": d.expected_flag,
            "goal": next((f.description for f in detail.facts if f.id == "goal"), ""),
            "assigned_intent": {"id": intent_id, "description": assigned.description if assigned else ""},
            "assigned_intent_sources": assigned.from_ if assigned else ["origin"],
            "latest_fact_id": detail.facts[-1].id if detail.facts else "origin",
            "facts": [{"id": f.id, "description": f.description} for f in detail.facts],
            "hints": [
                {"id": h.id, "content": h.content, "creator": h.creator, "created_at": h.created_at}
                for h in detail.hints
            ],
            "open_intents": [
                {"id": i.id, "description": i.description}
                for i in detail.intents if i.to is None
            ],
            "exposed_tools": exposed,
            "member_tool_inventory": member_tool_inventory(),
            "member_tool_inventory_source": {
                "backend_path": member_tool_inventory_path(),
                "workspace_path": "tools.txt",
                "docker_path": "/tools.txt",
                "note": "Use bash `cat tools.txt`; in Docker sandboxes, `/tools.txt` is also available.",
            },
            "runtime_cli_tools": {
                "available": [name for name, ok in cli_availability.items() if ok],
                "missing": [name for name, ok in cli_availability.items() if not ok],
            },
            "available_mcps": d.mcps.names(),
            "public_mcps": list(PUBLIC_MCPS),
            "available_languages": list(LANGUAGES),
            "attachment_true": attachment_true,
            "attachments": attachments,
            "recent_observations": self.observations[-6:],
            "stuckness_state": {
                "recent_action_signatures": len(self._recent_action_sigs),
                "loop_window": 12,
                "warn_threshold": 3,
                "break_threshold": 5,
            },
            "pending_bumps": pending_bumps,
            "bump_insights": sibling_insights,
            "previous_attempts": previous_attempts[-5:],
        }

    def _observe_webui_links(self, project_id: str, command: str, stdout: str, stderr: str) -> None:
        expose = getattr(self.deps.sandbox, "expose_webui", None)
        if not callable(expose):
            return
        ports = self._discover_webui_ports(command, stdout, stderr)
        for port in sorted(ports):
            try:
                url = expose(project_id, self.name, port)
            except Exception:
                continue
            self._observe(
                f"[webui:{port}] Shared browser URL: {url}/ "
                f"(open with MCP browser.navigate or browser.screenshot)"
            )

    def _discover_webui_ports(self, command: str, stdout: str, stderr: str) -> set[int]:
        ports = {int(match.group(1)) for match in _LOCAL_WEBUI_URL_RE.finditer(f"{stdout}\n{stderr}\n{command}")}
        if ports:
            return {port for port in ports if 1 <= port <= 65535}
        if not _WEBUI_HINT_RE.search(command):
            return set()
        return {
            int(match.group(1))
            for match in _PORT_FLAG_RE.finditer(command)
            if 1 <= int(match.group(1)) <= 65535
        }

    def _observe(self, text: str) -> None:
        self.observations.append(text[:2000])

    def _string_arg(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)
