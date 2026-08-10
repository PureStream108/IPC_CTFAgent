from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings

from mcp.server.fastmcp import Context

from backend.blackboard import edge_store, graph_store
from backend.core.config import CATEGORIES
from backend.core.ipc import (
    FlagConflictError,
    accept_verified_flag,
    assert_flag_compatible,
)
from backend.core.postprocess_store import (
    complete_existing_job,
    enqueue_postprocess,
)
from backend.core.state import AppState
from backend.core.wp_writer import persist_validated_writeup, write_wp_content
from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.ops.store import OpsStore
from backend.ops.tools import OpsToolError, OpsToolExecutor

_SESSION_ID_RE = re.compile(r"^ops_[a-f0-9]{16}$")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)\b(?:authorization|api[ _-]?key|token|cookie|password|secret)\b\s*[:=]\s*[^\s,;]+"
)


def build_ipc_mcp(state_provider: Callable[[], AppState]) -> MCPServer:
    """Build the IPC lifecycle MCP exposed to the Claude Code sidecar.

    This server is intentionally separate from Claude's native prompt.  Tool
    descriptions are the contract: start IPC's distributed solver before CTF
    work, record material actions in its logs, and finalize it with a Markdown writeup.  The
    app state is obtained lazily because the enclosing FastAPI application only
    creates it during its lifespan.
    """

    server = create_mcp_server(
        "ipc",
        """Operate the live IPC CTF workspace. For a CTF challenge, call
ipc_start_challenge before analysis so that activity is linked to a project and
the distributed IPC Members are queued. Use ipc_solver_status to observe Docker
startup and Member dispatch, and ipc_stop_challenge to interrupt them.
Use ipc_project_activity for material findings. When a flag is verified, call
ipc_finalize_challenge with the flag and a complete Markdown writeup; this is
what makes the IPC Logs and WP export pages contain the completed work. Do not
put API keys, cookies, tokens, or passwords in activity summaries. The host
tool is intentionally host-root and should only be used for an explicit
operator request.""",
    )
    tools = _Tools(state_provider)

    @server.tool(
        name="ipc_list_projects",
        description="List live IPC projects and their status, including whether a task sandbox is active.",
    )
    def list_projects() -> dict[str, Any]:
        return tools.list_projects()

    @server.tool(
        name="ipc_start_challenge",
        description=(
            "Create and link a live IPC project for the current CTF task, then queue IPC's "
            "distributed Members without blocking on Docker startup."
        ),
    )
    def start_challenge(
        title: str,
        category: str,
        origin: str,
        goal: str = "Solve the challenge and document a reproducible writeup.",
        external_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        return tools.start_challenge(
            title=title,
            category=category,
            origin=origin,
            goal=goal,
            external_id=external_id,
            session_id=_session_id_from_context(ctx),
        )

    @server.tool(
        name="ipc_start_solver",
        description="Queue IPC's distributed Members for an existing project and return its startup phase immediately.",
    )
    def start_solver(project_id: str) -> dict[str, Any]:
        return tools.start_solver(project_id)

    @server.tool(
        name="ipc_solver_status",
        description="Inspect project status, startup phase/error, task-sandbox availability, and active Members.",
    )
    def solver_status(project_id: str) -> dict[str, Any]:
        return tools.solver_status(project_id)

    @server.tool(
        name="ipc_stop_challenge",
        description="Interrupt IPC Members, stop the task sandbox, and leave the project resumable.",
    )
    def stop_challenge(project_id: str) -> dict[str, Any]:
        return tools.stop_challenge(project_id)

    @server.tool(
        name="ipc_project_activity",
        description=(
            "Record a concise, non-sensitive CTF finding or decision in an IPC project's "
            "durable project log."
        ),
    )
    def project_activity(project_id: str, event: str, summary: str) -> dict[str, Any]:
        return tools.project_activity(project_id, event, summary)

    @server.tool(
        name="ipc_write_writeup",
        description=(
            "Save a Markdown writeup for an IPC project without marking the challenge complete. "
            "Use ipc_finalize_challenge after a flag is verified."
        ),
    )
    def write_writeup(project_id: str, markdown: str) -> dict[str, Any]:
        return tools.write_writeup(project_id, markdown)

    @server.tool(
        name="ipc_finalize_challenge",
        description=(
            "Record a verified flag, save the complete Markdown writeup, and mark the IPC project "
            "completed so it appears in WP export."
        ),
    )
    def finalize_challenge(project_id: str, flag: str, markdown: str) -> dict[str, Any]:
        return tools.finalize_challenge(project_id, flag, markdown)

    @server.tool(
        name="ipc_task_sandbox_health",
        description="Run a harmless health probe in an active IPC task sandbox and record its result.",
    )
    def task_sandbox_health(project_id: str) -> dict[str, Any]:
        return tools.task_sandbox_health(project_id)

    @server.tool(
        name="ipc_task_sandbox_exec",
        description=(
            "Run a command as the task-container user in an active IPC task sandbox and record "
            "command metadata in the project log."
        ),
    )
    def task_sandbox_exec(project_id: str, command: str, timeout: int = 60) -> dict[str, Any]:
        return tools.task_sandbox_exec(project_id, command, timeout)

    @server.tool(
        name="ipc_host_exec",
        description=(
            "Execute a command as root on the Docker host. This is the highest-privilege IPC tool; "
            "use it only when the operator explicitly requested a host-level diagnostic or change."
        ),
    )
    def host_exec(command: str, timeout: int = 60) -> dict[str, Any]:
        return tools.host_exec(command, timeout)

    # The runner reaches this server over the compose network as ipc-app:8000.
    # Retain FastMCP's DNS-rebinding checks while accepting that internal host.
    security = server.settings.transport_security
    if security is None:
        security = TransportSecuritySettings()
        server.settings.transport_security = security
    if "ipc-app:8000" not in security.allowed_hosts:
        security.allowed_hosts.append("ipc-app:8000")
    if "testserver" not in security.allowed_hosts:
        security.allowed_hosts.append("testserver")
    return server


class _Tools:
    def __init__(self, state_provider: Callable[[], AppState]) -> None:
        self._state_provider = state_provider

    @property
    def state(self) -> AppState:
        return self._state_provider()

    def list_projects(self) -> dict[str, Any]:
        return OpsToolExecutor(self.state).list_task_sandboxes()

    def start_challenge(
        self,
        *,
        title: str,
        category: str,
        origin: str,
        goal: str,
        external_id: str | None,
        session_id: str | None,
    ) -> dict[str, Any]:
        normalized_category = str(category).strip().lower()
        if normalized_category not in CATEGORIES:
            return {"ok": False, "error": f"category must be one of {', '.join(CATEGORIES)}"}
        startup_error = self._startup_error()
        if startup_error:
            return {"ok": False, "error": startup_error}
        title = _required_text(title, "title", 240)
        origin = _required_text(origin, "origin", 12_000)
        goal = _required_text(goal, "goal", 12_000)
        ext = str(external_id).strip()[:240] if external_id else None
        with self.state.db.connect() as conn:
            project_id = graph_store.create_project(
                conn, title, origin, goal, normalized_category, external_id=ext
            )
        self.state.logger.project(
            "ops_agent_project_started",
            project_id,
            title=title,
            category=normalized_category,
            source="claude-code-mcp",
        )
        if session_id:
            OpsStore(self.state.root, self.state.db).link_session_project(session_id, project_id)
        result = self.start_solver(project_id)
        result["message"] = (
            "Project is linked and IPC solver startup is queued. Poll ipc_solver_status, "
            "record findings with ipc_project_activity, and finalize with ipc_finalize_challenge."
        )
        return result

    def start_solver(self, project_id: str) -> dict[str, Any]:
        self._require_project(project_id)
        startup_error = self._startup_error()
        if startup_error:
            return {"ok": False, "project_id": project_id, "error": startup_error}
        try:
            status = self.state.orchestrator.start_project_async(project_id)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "project_id": project_id, "error": str(exc)}
        return {"ok": True, **status}

    def solver_status(self, project_id: str) -> dict[str, Any]:
        self._require_project(project_id)
        if self.state.orchestrator is not None:
            status = self.state.orchestrator.runtime_status(project_id)
        else:
            with self.state.db.connect() as conn:
                row = graph_store.get_project_row(conn, project_id)
            status = {
                "project_id": project_id,
                "status": str(row["status"]),
                "phase": str(row["runtime_phase"] or "idle"),
                "error": row["runtime_error"],
            }
        with self.state.db.connect() as conn:
            active_members = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM agents WHERE project_id = %s AND role = 'member' AND state = 'active' ORDER BY name",
                    (project_id,),
                ).fetchall()
            ]
        return {
            "ok": True,
            **status,
            "sandbox_active": project_id in set(self.state.pool.active_projects()),
            "active_members": active_members,
        }

    def stop_challenge(self, project_id: str) -> dict[str, Any]:
        self._require_project(project_id)
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
            if row["status"] == "solved":
                return {"ok": False, "project_id": project_id, "error": "solved projects cannot be stopped"}
            graph_store.set_status(conn, project_id, "stopped")
            graph_store.set_runtime_phase(conn, project_id, "stopped")
            conn.execute(
                "UPDATE intents SET worker = NULL, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, last_heartbeat_at = NULL "
                "WHERE project_id = %s AND concluded_at IS NULL",
                (project_id,),
            )
            graph_store.clear_reason(conn, project_id)
        if self.state.orchestrator is not None:
            self.state.orchestrator.stop_project(project_id)
        self.state.logger.project("ops_agent_project_stopped", project_id, source="claude-code-mcp")
        return {"ok": True, "project_id": project_id, "status": "stopped", "phase": "stopped"}

    def project_activity(self, project_id: str, event: str, summary: str) -> dict[str, Any]:
        self._require_project(project_id)
        event = _required_text(event, "event", 120)
        summary = _safe_summary(summary)
        self.state.logger.llm(
            "ops_agent_activity",
            project_id,
            source="claude-code-mcp",
            activity=event,
            summary=summary,
        )
        return {"ok": True, "project_id": project_id, "event": event}

    def write_writeup(self, project_id: str, markdown: str) -> dict[str, Any]:
        self._require_project(project_id)
        try:
            path = write_wp_content(self.state.db, project_id, self.state.wp_dir, markdown)
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        self.state.logger.project("ops_agent_writeup_saved", project_id, path=path)
        return {"ok": True, "project_id": project_id, "wp_path": path}

    def finalize_challenge(self, project_id: str, flag: str, markdown: str) -> dict[str, Any]:
        self._require_project(project_id)
        try:
            flag = _required_text(flag, "flag", 2048)
        except ValueError as exc:
            return {"ok": False, "project_id": project_id, "error": str(exc)}
        rollback_file: Callable[[], None] | None = None
        try:
            with self.state.db.connect() as conn:
                # Hold the project row lock across the compatibility check,
                # filesystem write, graph completion, and Flag commit.  A
                # losing concurrent finalizer fails before touching the WP.
                assert_flag_compatible(conn, project_id, flag)
                path, rollback_file = persist_validated_writeup(
                    conn,
                    project_id,
                    self.state.wp_dir,
                    markdown,
                    expected_flag=flag,
                )
                source = conn.execute(
                    "SELECT id FROM facts WHERE project_id = %s AND id <> 'goal' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                source_id = source["id"] if source else "origin"
                edge_store.complete_goal_intent(
                    conn,
                    project_id,
                    [source_id],
                    "flag captured by IPC action agent",
                    "ops-agent",
                    "ops-agent",
                )
                accept_verified_flag(
                    conn,
                    project_id,
                    flag,
                    source="ops-agent",
                )
                enqueue_postprocess(conn, project_id)
                complete_existing_job(conn, project_id, "writeup")
                graph_store.set_runtime_phase(conn, project_id, "solved")
        except FlagConflictError as exc:
            return {"ok": False, "project_id": project_id, "conflict": True, "error": str(exc)}
        except (RuntimeError, ValueError) as exc:
            if rollback_file is not None:
                rollback_file()
            return {"ok": False, "project_id": project_id, "error": str(exc)}
        except Exception:
            if rollback_file is not None:
                rollback_file()
            raise
        if self.state.orchestrator is not None:
            self.state.orchestrator.on_flag_found(project_id)
        with self.state.db.connect() as conn:
            project = graph_store.get_project_row(conn, project_id)
        postprocess_status = project["postprocess_status"] if project is not None else "pending"
        self.state.logger.project(
            "ops_agent_challenge_finalized",
            project_id,
            source="claude-code-mcp",
            wp_path=path,
            flag_captured=True,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "status": "solved",
            "postprocess_status": postprocess_status,
            "wp_path": path,
            "message": "Flag verified. Memory and archive processing continue asynchronously.",
        }

    def task_sandbox_health(self, project_id: str) -> dict[str, Any]:
        return self._run_project_tool(project_id, "task_sandbox_health")

    def task_sandbox_exec(self, project_id: str, command: str, timeout: int) -> dict[str, Any]:
        return self._run_project_tool(project_id, "task_sandbox_exec", project_id, command, timeout)

    def host_exec(self, command: str, timeout: int) -> dict[str, Any]:
        try:
            result = OpsToolExecutor(self.state).host_exec(command, timeout)
        except OpsToolError as exc:
            return {"ok": False, "error": str(exc), "privilege": "host-root"}
        self.state.logger.tool(
            "ops_agent_host_exec",
            "global",
            source="claude-code-mcp",
            privilege="host-root",
            ok=bool(result.get("ok")),
            exit_code=result.get("exit_code"),
            command_length=len(command) if isinstance(command, str) else 0,
        )
        return result

    def _run_project_tool(self, project_id: str, name: str, *args: Any) -> dict[str, Any]:
        self._require_project(project_id)
        executor = OpsToolExecutor(self.state)
        try:
            result = executor.execute(name, {"project_id": project_id, **_arguments_for(name, args)})
        except OpsToolError as exc:
            result = {"ok": False, "error": str(exc), "project_id": project_id}
        self.state.logger.tool(
            "ops_agent_task_exec",
            project_id,
            source="claude-code-mcp",
            tool=name,
            ok=bool(result.get("ok")),
            exit_code=result.get("exit_code"),
            command_length=(len(args[0]) if name == "task_sandbox_exec" and isinstance(args[0], str) else 0),
        )
        return result

    def _require_project(self, project_id: str) -> None:
        with self.state.db.connect() as conn:
            row = graph_store.get_project_row(conn, project_id)
        if row is None:
            raise ValueError(f"project not found: {project_id}")

    def _startup_error(self) -> str | None:
        errors = self.state.config.startup_errors()
        if errors:
            return "; ".join(errors)
        if self.state.orchestrator is None:
            return "IPC orchestrator is not running"
        return None


def _arguments_for(name: str, args: tuple[Any, ...]) -> dict[str, Any]:
    if name == "task_sandbox_health":
        return {}
    if name == "task_sandbox_exec":
        command, timeout = args
        return {"command": command, "timeout": timeout}
    raise ValueError(f"unexpected IPC tool: {name}")


def _required_text(value: Any, field: str, maximum: int) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} is limited to {maximum} characters")
    return text


def _safe_summary(value: Any) -> str:
    text = _required_text(value, "summary", 12_000)
    return _SENSITIVE_FIELD_RE.sub("[redacted-sensitive-field]", text)


def _session_id_from_context(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
        headers = getattr(request, "headers", None)
        value = headers.get("x-ipc-ops-session", "") if headers is not None else ""
    except Exception:
        return None
    return value if isinstance(value, str) and _SESSION_ID_RE.fullmatch(value) else None
