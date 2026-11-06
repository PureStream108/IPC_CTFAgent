from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
import threading
from pathlib import Path
from typing import Any

from backend.blackboard import graph_store
from backend.ops.attachments import UPLOAD_ID_RE, OpsAttachmentStore
from backend.platform.presets import preset_tool_definitions, template_for_tool
from backend.sandbox.docker_manager import _load_docker_sdk
from backend.sandbox.guard import command_guard

_recovery_logger = logging.getLogger(__name__)

class OpsToolError(RuntimeError):
    """A tool call was invalid or could not be completed."""


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ATTACHMENT_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_ACTIVE_PROJECT_STATES = {"running", "flag_found"}
_OPS_MEMBER = "ops-agent"
_MAX_COMMAND_LENGTH = 32_000
_MAX_OUTPUT_LENGTH = 16_000
_COMPETITION_LOCK = threading.Lock()


_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "question",
        "description": "Ask for missing platform information. Stop the dependent operation until answered; reuse the same operation_key when resuming. For credentials specify workflow_id and secret_name, never ask for credentials in chat.",
        "parameters": {
            "type": "object",
            "properties": {"operation_key": {"type": "string"}, "title": {"type": "string"},
                           "options": {"type": "array", "items": {"type": "string"}},
                           "workflow_id": {"type": "string"}, "secret_name": {"type": "string"}},
            "required": ["operation_key", "title"], "additionalProperties": False,
        },
    },
    # One IPC tool per built-in platform preset; the platform package owns the
    # tool schema and configure() implementation.
    *preset_tool_definitions(),
    {
        "name": "platform_preflight",
        "description": (
            "Read a platform workflow's challenge list and solved flags. No confirmation "
            "phrase is required: Start is the operator's own click. "
            "This only queries the platform adapter; it does not create Projects, "
            "CompetitionRuns, task sandboxes, or Member assignments. Use it before "
            "platform_start to decide which challenges remain."
        ),
        "parameters": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "platform_start",
        "description": (
            "Start one platform workflow. No confirmation phrase is required. This creates "
            "the CompetitionRun, "
            "imports every challenge Project, marks externally solved challenges, and "
            "queues the remaining challenges for Member allocation. Task sandboxes are "
            "created later by Project startup; never require an existing sandbox or ask "
            "the operator to create one first. idempotency_key must be at least 8 chars; "
            "select optionally limits the run to specific external challenge ids."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "idempotency_key": {"type": "string", "minLength": 8},
                "select": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional external challenge ids; omit for all challenges.",
                },
            },
            "required": ["workflow_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "platform_status",
        "description": (
            "Read a CompetitionRun snapshot and its current Member seats. Use it after "
            "platform_start to report which challenges were imported, solved elsewhere, "
            "or assigned."
        ),
        "parameters": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_task_sandboxes",
        "description": "List CTF projects and whether their task sandbox is active.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "publish_upload",
        "description": (
            "Copy one operator chat upload into a project's task sandbox at "
            "/workspace/attachments/<filename>. Use this before reading a binary or "
            "zip attachment: uploads live in the app, so no task container can see "
            "them until they are published here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "upload_id": {"type": "string"},
                "project_id": {"type": "string"},
                "name": {
                    "type": "string",
                    "description": "Optional destination filename; defaults to the uploaded name.",
                },
            },
            "required": ["upload_id", "project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_sandbox_health",
        "description": "Run a harmless health probe inside an active task container.",
        "parameters": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_sandbox_exec",
        "description": (
            "Execute a command as the task-container user in the active CTF task "
            "workspace. Use this for challenge files and task-image tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "command": {"type": "string", "maxLength": _MAX_COMMAND_LENGTH},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["project_id", "command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "host_exec",
        "description": (
            "Execute a command as root on the Docker host. This is the highest-privilege "
            "maintenance tool: it can read or modify the host filesystem, processes, "
            "containers, and network. Only use it when the operator explicitly asks "
            "for host-level diagnostics or changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "maxLength": _MAX_COMMAND_LENGTH},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "dangerous": True,
    },
)


def tool_definitions() -> list[dict[str, Any]]:
    """Return a detached, JSON-safe copy of the IPC tool catalogue."""

    return copy.deepcopy(list(_TOOL_DEFINITIONS))


def tool_prompt() -> str:
    return json.dumps(tool_definitions(), ensure_ascii=False, separators=(",", ":"))


class OpsToolExecutor:
    """Execute IPC's deliberately privileged tools.

    ``task_sandbox_exec`` stays inside an admitted task container.  ``host_exec``
    is intentionally stronger: the application already owns the Docker socket,
    so it launches a short-lived privileged helper with the host root mounted at
    ``/host`` and runs the requested command through ``chroot``.  There is no
    useful way to make that capability safe from a compromised model; the
    authenticated IPC action agent is therefore treated as a host administrator.
    """

    def __init__(self, state) -> None:
        self.state = state

    def catalog(self) -> list[dict[str, Any]]:
        return tool_definitions()

    def execute(self, name: str, arguments: dict[str, Any] | None = None, *, session_id: str | None = None) -> dict[str, Any]:
        args = arguments or {}
        if not isinstance(args, dict):
            raise OpsToolError("tool arguments must be an object")
        if name == "question":
            from backend.ops.questions import QuestionStore
            if not session_id:
                raise OpsToolError("question requires a conversation session")
            try:
                return QuestionStore(self.state).create(session_id=session_id, **args)
            except (ValueError, TypeError, RuntimeError) as exc:
                raise OpsToolError(str(exc)) from exc
        template_id = template_for_tool(name)
        if template_id is not None:
            return self._configure_preset(template_id, args)
        handlers = {
            "platform_preflight": self.platform_preflight,
            "platform_start": self.platform_start,
            "platform_status": self.platform_status,
            "list_task_sandboxes": self.list_task_sandboxes,
            "task_sandbox_health": self.task_sandbox_health,
            "task_sandbox_exec": self.task_sandbox_exec,
            "publish_upload": self.publish_upload,
            "host_exec": self.host_exec,
        }
        handler = handlers.get(name)
        if handler is None:
            raise OpsToolError(f"unknown IPC tool: {name}")
        try:
            return handler(**args)
        except TypeError as exc:
            raise OpsToolError(f"invalid arguments for {name}: {exc}") from exc

    def _configure_preset(
        self, template_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Create or update one native preset through the ops service."""

        unknown = set(arguments) - {"settings", "workflow_id"}
        if unknown:
            raise OpsToolError(
                f"invalid arguments for the {template_id} preset: {sorted(unknown)}"
            )
        settings = arguments.get("settings")
        if not isinstance(settings, dict):
            raise OpsToolError("settings must be an object")
        if "secrets" in settings:
            raise OpsToolError(
                "credentials must use question with workflow_id and secret_name, not settings"
            )
        workflow_id = arguments.get("workflow_id")
        if workflow_id is not None and not isinstance(workflow_id, str):
            raise OpsToolError("workflow_id must be a string")
        service = getattr(self.state, "ops_agent_service", None)
        if service is None:
            from backend.ops.service import OpsAgentService

            service = OpsAgentService(self.state)
        configure = getattr(service, "configure_preset_workflow", None)
        if configure is None:
            raise OpsToolError("the preset is not available")
        try:
            return configure(template_id, settings, workflow_id=workflow_id)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise OpsToolError(str(exc)) from exc

    def _competition(self):
        """Return one process-wide CompetitionService, creating it lazily."""
        service = getattr(self.state, "competition", None)
        if service is not None:
            return service
        with _COMPETITION_LOCK:
            service = getattr(self.state, "competition", None)
            if service is None:
                from backend.competition.service import CompetitionService

                service = CompetitionService(self.state)
                self.state.competition = service
                service.start_worker()
        return service

    def platform_preflight(self, workflow_id: str) -> dict[str, Any]:
        """Read platform challenges without creating a run or a sandbox."""
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            raise OpsToolError("workflow_id is required")
        try:
            result = self._competition().preflight(workflow_id)
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            raise OpsToolError(str(exc)) from exc
        return {"ok": True, **result}

    def platform_start(
        self,
        workflow_id: str,
        idempotency_key: str,
        select: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create the CompetitionRun and import/schedule its challenges."""
        workflow_id = str(workflow_id or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not workflow_id:
            raise OpsToolError("workflow_id is required")
        if len(idempotency_key) < 8:
            raise OpsToolError("idempotency_key must contain at least 8 characters")
        selected = [str(item) for item in select] if select is not None else None
        try:
            snapshot = self._competition().start(
                workflow_id, idempotency_key, select=selected
            )
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            raise OpsToolError(str(exc)) from exc
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "run": snapshot.get("run", snapshot),
            "capacity": snapshot.get("capacity"),
        }

    def platform_status(self, run_id: str) -> dict[str, Any]:
        """Read the durable CompetitionRun and Member seat snapshot."""
        run_id = str(run_id or "").strip()
        if not run_id:
            raise OpsToolError("run_id is required")
        try:
            service = self._competition()
            run = service.store.run_snapshot(run_id)
            members = service.store.member_snapshot(run_id)
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            raise OpsToolError(str(exc)) from exc
        return {"ok": True, "run": run, "members": members}

    def list_task_sandboxes(self) -> dict[str, Any]:
        active = set(self.state.pool.active_projects())
        with self.state.db.connect() as connection:
            rows = connection.execute(
                "SELECT id, title, category, status, runtime_phase, runtime_error, updated_at "
                "FROM projects ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        return {
            "projects": [
                {
                    "project_id": row["id"],
                    "title": row["title"],
                    "category": row["category"],
                    "status": row["status"],
                    "phase": row["runtime_phase"],
                    "error": row["runtime_error"],
                    "sandbox_active": row["id"] in active,
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        }

    def task_sandbox_health(self, project_id: str) -> dict[str, Any]:
        sandbox = self._task_sandbox(project_id)
        result = sandbox.exec(
            "printf '%s\\n' __IPC_TASK_SANDBOX_OK__; "
            "printf 'workdir=%s\\n' \"$PWD\"; "
            "command -v bash || true; command -v python3 || true; command -v timeout || true",
            timeout=15,
        )
        return self._exec_result(result, sandbox=sandbox, project_id=project_id)

    def task_sandbox_exec(
        self,
        project_id: str,
        command: str,
        timeout: int = 60,
    ) -> dict[str, Any]:
        command, timeout = _validate_command(command, timeout)
        decision = command_guard.check(command, kind="bash")
        if decision.blocked:
            raise OpsToolError(
                f"task sandbox commands must stay inside the container: {decision.reason}"
            )
        sandbox = self._task_sandbox(project_id)
        result = sandbox.exec(command, timeout=timeout)
        return self._exec_result(result, sandbox=sandbox, project_id=project_id)

    def publish_upload(
        self, upload_id: str, project_id: str, name: str = ""
    ) -> dict[str, Any]:
        """Copy one operator upload into a project's task sandbox."""

        if not isinstance(upload_id, str) or not upload_id.strip():
            raise OpsToolError("upload_id is required")
        upload_id = upload_id.strip()
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise OpsToolError("upload_id must look like upload_<24 hex characters>")
        sandbox = self._task_sandbox(project_id)
        service = getattr(self.state, "ops_agent_service", None)
        if service is None:
            raise OpsToolError("the ops service is not running")
        store = OpsAttachmentStore(service.store.root)
        try:
            metadata = store.metadata(upload_id)
        except KeyError as exc:
            raise OpsToolError(f"unknown upload: {upload_id}") from exc
        source = store.root / f"{upload_id}.bin"
        if not source.is_file():
            raise OpsToolError(f"upload {upload_id} has no stored bytes")
        filename = _attachment_name(name or str(metadata.get("filename") or ""))
        target = Path(self.state.attachments_dir(project_id)) / filename
        data = source.read_bytes()
        target.write_bytes(data)
        copied = sandbox.publish([target])
        return {
            "ok": True,
            "upload_id": upload_id,
            "project_id": project_id,
            "filename": filename,
            "bytes": len(data),
            "sandbox_path": f"/workspace/attachments/{filename}",
            "copied_into_container": copied > 0,
        }

    def host_exec(self, command: str, timeout: int = 60) -> dict[str, Any]:
        command, timeout = _validate_command(command, timeout)
        docker = _load_docker_sdk()
        client = docker.from_env()
        image = _host_helper_image(client)
        wrapped = (
            f"timeout -k 5s {timeout}s "
            f"chroot /host /bin/bash -lc {shlex.quote(command)}"
        )
        container = None
        try:
            container = client.containers.run(
                image=image,
                command=["sh", "-lc", wrapped],
                remove=False,
                detach=True,
                privileged=True,
                pid_mode="host",
                network_mode="host",
                volumes={"/": {"bind": "/host", "mode": "rw"}},
            )
            wait_result = container.wait(timeout=timeout + 15)
            status_code = int(wait_result.get("StatusCode", 1)) if isinstance(wait_result, dict) else 1
            output = container.logs(stdout=True, stderr=True)
        except Exception as exc:
            raise OpsToolError(f"host command failed to start: {type(exc).__name__}: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    _recovery_logger.debug('Error handled in host_exec', exc_info=True)

        stdout, stderr = _split_output(output)
        return {
            "ok": status_code == 0,
            "privilege": "host-root",
            "image": image,
            "exit_code": status_code,
            "timed_out": status_code in (124, 137),
            "stdout": _clip(stdout),
            "stderr": _clip(stderr),
        }

    def _task_sandbox(self, project_id: str):
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            raise OpsToolError("project_id must be a simple project identifier")
        with self.state.db.connect() as connection:
            row = graph_store.get_project_row(connection, project_id)
        if row is None:
            raise OpsToolError(f"project not found: {project_id}")
        if row["status"] not in _ACTIVE_PROJECT_STATES:
            raise OpsToolError(
                f"project {project_id} is {row['status']}; start the project before using its task sandbox"
            )
        if project_id not in set(self.state.pool.active_projects()):
            raise OpsToolError(
                f"task sandbox for {project_id} is not active; start or resume the project first"
            )
        try:
            return self.state.pool.get(project_id, _OPS_MEMBER)
        except Exception as exc:
            raise OpsToolError(
                f"could not attach to task sandbox {project_id}: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _exec_result(result, *, sandbox, project_id: str) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "project_id": project_id,
            "sandbox": sandbox.name,
            "workdir": getattr(sandbox, "workdir", ""),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": _clip(result.stdout),
            "stderr": _clip(result.stderr),
        }


def _attachment_name(value: str) -> str:
    name = Path(str(value or "attachment")).name
    name = _ATTACHMENT_NAME_RE.sub("_", name).strip(" .")[:160]
    return name or "attachment"


def _validate_command(command: str, timeout: int) -> tuple[str, int]:
    if not isinstance(command, str) or not command.strip():
        raise OpsToolError("command must be a non-empty string")
    if "\x00" in command:
        raise OpsToolError("command cannot contain NUL bytes")
    if len(command) > _MAX_COMMAND_LENGTH:
        raise OpsToolError(f"command is limited to {_MAX_COMMAND_LENGTH} characters")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 120:
        raise OpsToolError("timeout must be an integer between 1 and 120 seconds")
    return command, timeout


def _host_helper_image(client) -> str:
    configured = os.environ.get("IPC_HOST_EXEC_IMAGE", "").strip()
    if configured:
        return configured
    preferred = "ipc-task:latest"
    try:
        client.images.get(preferred)
        return preferred
    except Exception:
        _recovery_logger.debug('Error handled in _host_helper_image', exc_info=True)
        return "ipc-ctfagent:latest"


def _split_output(output: Any) -> tuple[str, str]:
    if isinstance(output, tuple):
        stdout, stderr = output
    else:
        stdout, stderr = output, ""
    return _decode(stdout), _decode(stderr)


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _clip(value: str) -> str:
    if len(value) <= _MAX_OUTPUT_LENGTH:
        return value
    return value[:_MAX_OUTPUT_LENGTH] + f"\n[output clipped at {_MAX_OUTPUT_LENGTH} characters]"
