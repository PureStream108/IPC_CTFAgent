"""One provider tool per Member action, instead of a single ``member_action``.

The old shape asked the model to put the real verb in a string field
(``{"action": "bash", ...}``), which meant every argument had to be
``additionalProperties: true`` and the provider could not validate anything.
A separate tool per action gives each one a real schema, so a malformed call is
rejected by the provider rather than by ``MemberAction.from_obj`` after a
wasted round trip.

The second reason for the split is concurrency. With one opaque tool the
runtime had to serialize every call, because it could not tell a directory
listing from a flag submission. Here each tool declares whether it is safe to
run alongside its batch-mates:

* ``parallel_safe`` -- reads that cannot change the task's state. Several can
  run at once in a single turn.
* everything else -- side effects (``bash``, ``install``, MCP tools) and the
  terminal actions, which stay strictly ordered.

``bash`` is deliberately *not* parallel safe even though many commands are
harmless: the model supplies the command, so there is no way to know whether a
given one writes a file, restarts a service, or spends the challenge's only
rate-limited request.
"""
from __future__ import annotations

from typing import Any

from backend.core.validation import InputValidationError

TERMINAL_TOOLS = frozenset({"submit_flag", "conclude", "give_up"})

PARALLEL_SAFE_TOOLS = frozenset({"memory_search", "tool_search", "job_status", "job_logs"})

TOOL_ACTIONS: dict[str, str] = {
    "bash": "bash",
    "install": "install",
    "mcp_tool": "tool",
    "memory_search": "memory",
    "tool_search": "tool_search",
    "report": "report",
    "declare_intent": "intent",
    "conclude": "conclude",
    "submit_flag": "flag",
    "give_up": "done",
    "job_start": "job_start",
    "job_status": "job_status",
    "job_logs": "job_logs",
    "job_cancel": "job_cancel",
}


def _tool(
    name: str, description: str, properties: dict[str, Any],
    required: list[str] | None = None, *, additional: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": additional,
        },
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}


MEMBER_TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "bash",
        "Run one shell command in this challenge's container. Killed after 60 "
        "seconds; use job_start for anything slower.",
        {"command": {**_STRING, "description": "The command to run."},
         "thought": _STRING},
        ["command"],
    ),
    _tool(
        "install",
        "Install missing packages into this container.",
        {
            "manager": {"type": "string", "enum": ["apt", "pip", "gem", "npm", "go"]},
            "packages": {"type": "array", "items": _STRING},
            "thought": _STRING,
        },
        ["packages"],
    ),
    _tool(
        "mcp_tool",
        "Call a tool exposed by an MCP server.",
        {
            "server": _STRING,
            "tool": _STRING,
            "args": {"type": "object", "additionalProperties": True},
            "thought": _STRING,
        },
        ["server", "tool"],
    ),
    _tool(
        "memory_search",
        "Search durable memory for experience from past challenges.",
        {"query": _STRING, "thought": _STRING},
        ["query"],
    ),
    _tool(
        "tool_search",
        "Search the tool catalog for a capability you do not have loaded.",
        {"query": _STRING, "thought": _STRING},
        ["query"],
    ),
    _tool(
        "report",
        "Report difficulty to Diamond so it can re-plan.",
        {
            "progress": _STRING,
            "difficulty": {"type": "string", "enum": ["low", "medium", "high", "ex"]},
            "steps": {"type": "array", "items": _STRING},
            "directions": {"type": "array", "items": _STRING},
            "knowledge": {"type": "array", "items": _STRING},
            "thought": _STRING,
        },
        ["progress", "difficulty"],
    ),
    _tool(
        "declare_intent",
        "Declare a new exploration intent branching from a confirmed fact.",
        {"description": _STRING, "from": _STRING, "thought": _STRING},
        ["description"],
    ),
    _tool(
        "conclude",
        "Record a confirmed fact for your assigned intent and finish it.",
        {"description": _STRING, "thought": _STRING},
        ["description"],
    ),
    _tool(
        "submit_flag",
        "Submit a flag you have actually verified. Never guess.",
        {
            "flag": _STRING,
            "description": {
                **_STRING,
                "description": "The evidence chain that produced this flag.",
            },
            "from": _STRING,
            "thought": _STRING,
        },
        ["flag", "description"],
    ),
    _tool(
        "give_up",
        "Stop work on this intent because the evidence is genuinely exhausted.",
        {"reason": _STRING, "thought": _STRING},
        ["reason"],
    ),
    _tool(
        "job_start",
        "Start a command with no timeout, for brute force, large file analysis "
        "or slow scans. Poll it with job_status and job_logs.",
        {"command": _STRING, "thought": _STRING},
        ["command"],
    ),
    _tool(
        "job_status",
        "Check one job, or list this session's jobs when job_id is omitted. A "
        "job reported as lost has an unknown result, never a successful one.",
        {"job_id": _STRING, "thought": _STRING},
    ),
    _tool(
        "job_logs",
        "Read a job's output incrementally. Omit offset to continue from where "
        "you last read.",
        {
            "job_id": _STRING,
            "stream": {"type": "string", "enum": ["stdout", "stderr"]},
            "offset": _INTEGER,
            "limit": _INTEGER,
            "thought": _STRING,
        },
        ["job_id"],
    ),
    _tool(
        "job_cancel",
        "Kill a running job and its whole process tree.",
        {"job_id": _STRING, "thought": _STRING},
        ["job_id"],
    ),
)

MEMBER_TOOL_NAMES = tuple(tool["name"] for tool in MEMBER_TOOLS)


def is_parallel_safe(name: str) -> bool:
    return name in PARALLEL_SAFE_TOOLS


def is_terminal(name: str) -> bool:
    return name in TERMINAL_TOOLS


def action_payload(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Translate a native tool call into ``MemberAction.from_obj`` input.

    The split removes the need for most of the old normalisation - each tool
    has a schema, so aliases like ``cmd`` for ``command`` no longer have to be
    guessed. What remains is the ``args`` unwrapping, because models still
    sometimes nest arguments one level deep out of habit from the single-tool
    shape.
    """
    if name not in TOOL_ACTIONS:
        raise ValueError(f"unsupported provider tool: {name}")
    raw = dict(arguments or {})
    nested = raw.pop("args", None) if name != "mcp_tool" else None
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update(raw)
        raw = merged
    raw["action"] = TOOL_ACTIONS[name]
    return raw


ACTION_TOOLS: dict[str, str] = {action: name for name, action in TOOL_ACTIONS.items()}


def tool_call_for_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a legacy action dict onto a ``(tool_name, arguments)`` pair.

    This is the exact inverse of :func:`action_payload`: the arguments it
    returns are what the native executor will feed back through
    ``action_payload`` and ``MemberAction.from_obj``, so a converted script
    exercises the same side-effect path as the legacy driver did. ``thought``
    rides along because every tool schema accepts it.
    """
    if not isinstance(action, dict):
        raise InputValidationError(f"action must be a dict, got {type(action).__name__}")
    kind = action.get("action") or action.get("kind")
    if kind not in ACTION_TOOLS:
        raise ValueError(f"no native tool maps to action kind: {kind!r}")
    arguments = {k: v for k, v in action.items() if k not in ("action", "kind")}
    return ACTION_TOOLS[kind], arguments


__all__ = [
    "ACTION_TOOLS",
    "MEMBER_TOOLS",
    "MEMBER_TOOL_NAMES",
    "PARALLEL_SAFE_TOOLS",
    "TERMINAL_TOOLS",
    "TOOL_ACTIONS",
    "action_payload",
    "is_parallel_safe",
    "is_terminal",
    "tool_call_for_action",
]
