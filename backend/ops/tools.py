from __future__ import annotations

import copy
import json
import os
import re
import shlex
from typing import Any

from backend.blackboard import graph_store
from backend.sandbox.docker_manager import _load_docker_sdk


class OpsToolError(RuntimeError):
    """A tool call was invalid or could not be completed."""


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ACTIVE_PROJECT_STATES = {"running", "flag_found", "wp_writing", "memory_writing"}
_OPS_MEMBER = "ops-agent"
_MAX_COMMAND_LENGTH = 32_000
_MAX_OUTPUT_LENGTH = 16_000


# These are deliberately provider-neutral. IPC's fallback adapter uses the
# JSON action protocol shared by all configured LLM adapters, so the same
# catalogue can be included in the prompt and exposed by the API/UI.
_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_task_sandboxes",
        "description": "List CTF projects and whether their task sandbox is active.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
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

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if not isinstance(args, dict):
            raise OpsToolError("tool arguments must be an object")
        handlers = {
            "list_task_sandboxes": self.list_task_sandboxes,
            "task_sandbox_health": self.task_sandbox_health,
            "task_sandbox_exec": self.task_sandbox_exec,
            "host_exec": self.host_exec,
        }
        handler = handlers.get(name)
        if handler is None:
            raise OpsToolError(f"unknown IPC tool: {name}")
        try:
            return handler(**args)
        except TypeError as exc:
            raise OpsToolError(f"invalid arguments for {name}: {exc}") from exc

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
        sandbox = self._task_sandbox(project_id)
        result = sandbox.exec(command, timeout=timeout)
        return self._exec_result(result, sandbox=sandbox, project_id=project_id)

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
            # Docker SDK 7.x does not accept ``demux`` on Container.logs();
            # combined output is sufficient for the host maintenance tool.
            output = container.logs(stdout=True, stderr=True)
        except Exception as exc:
            raise OpsToolError(f"host command failed to start: {type(exc).__name__}: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

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
        # The task image is intentionally large and may not have been built on
        # a fresh server yet.  The application image contains the shell and
        # core utilities needed by the host-root helper.
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
