from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

RET2SHELL_TEMPLATE_VERSION = 1
RET2SHELL_PERSONALIZATION = [
    {
        "key": "base_url",
        "label": "ret2shell platform URL",
        "kind": "url",
        "required": True,
        "help": "Scheme and host only, for example https://ctf.example",
    },
    {
        "key": "game_id",
        "label": "ret2shell game ID",
        "kind": "integer",
        "required": True,
        "help": "The numeric game id from the ret2shell game URL or game list",
    },
    {"key": "name", "label": "Competition name", "kind": "text", "required": False},
    {"key": "team_id", "label": "Team id or name", "kind": "text", "required": False},
    {"key": "ends_at", "label": "Deadline", "kind": "datetime", "required": False},
    {
        "key": "remote_concurrency",
        "label": "Remote container concurrency",
        "kind": "integer",
        "required": False,
    },
    {
        "key": "category_map",
        "label": "Category overrides",
        "kind": "object",
        "required": False,
    },
    {
        "key": "allow_private_networks",
        "label": "Allow private platform network",
        "kind": "boolean",
        "required": False,
    },
]
RET2SHELL_IPC_SETUP = {
    "tool": "ret2shell_workflow",
    "summary": (
        "Connect a ret2shell game and use the native challenge, attachment, submit "
        "and dynamic-instance APIs."
    ),
    "prompt": (
        "Set up the built-in ret2shell preset workflow. Use the ret2shell_workflow "
        "tool, not propose_platform_workflow. Ask me for the ret2shell platform URL "
        "and numeric game ID first; create the draft as soon as those identity "
        "fields are known, then ask for authentication. Ask one missing non-secret "
        "field at a time with the question tool. Never ask for a token or password "
        "in ordinary chat: after the draft exists, ask with the question tool, "
        "workflow_id and secret_name=ret2shell_token (or ret2shell_username/"
        "ret2shell_password). Do not invent values or probe the platform before I "
        "confirm the draft."
    ),
    "personalization": RET2SHELL_PERSONALIZATION,
}
PLATFORM = "ret2shell"
TOOL_NAME = "ret2shell_workflow"
MCP_NAME = "ret2shell"
RUNTIME_MCP_NOTE = " — use it with ret2shell MCP tools"
ALLOWED_SECRET_NAMES = frozenset(
    {
        "ret2shell_token",
        "ret2shell_username",
        "ret2shell_password",
        "platform_token",
    }
)
TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Create or update the built-in ret2shell preset workflow. Use this instead of "
        "propose_platform_workflow for ret2shell. Supply only non-secret settings; "
        "base_url and game_id are required. Ask the operator for missing values with the "
        "question tool, one field at a time. After the draft exists, ask for credentials "
        "with question, workflow_id and secret_name=ret2shell_token (or "
        "ret2shell_username/ret2shell_password). Never put credentials in settings or "
        "ordinary chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": "Ret2ShellWorkflowInput fields without secrets.",
                "additionalProperties": True,
            },
            "workflow_id": {"type": "string"},
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
}
MCP_TOOL_DESCRIPTION = (
    "Create or update the built-in default ret2shell workflow. Supply only the platform "
    "base_url, numeric game_id and optional name/team_id/ends_at/remote_concurrency. "
    "No HTTP mappings or platform probing are needed. Ask ipc_question for any missing "
    "base_url or game_id; after creating the draft, ask for ret2shell_token (or "
    "ret2shell_username/ret2shell_password) with ipc_question. Return the draft for "
    "normal confirmation."
)


def validate_mapping(mapping: Any) -> None:
    if not mapping.list_url.strip() or mapping.game_id is None:
        raise ValueError(
            "list_url and game_id are required for the ret2shell platform"
        )


TEMPLATE = {
    "id": "ret2shell",
    "name": "Ret2Shell",
    "version": 1,
    "default": False,
    "required": ["base_url", "game_id"],
    "optional": [
        "name",
        "team_id",
        "ends_at",
        "remote_concurrency",
        "category_map",
        "allow_private_networks",
    ],
    "secret_names": [
        "ret2shell_token",
        "ret2shell_username",
        "ret2shell_password",
    ],
    "capabilities": ["challenges", "attachments", "submit", "query", "instances"],
    "ipc_setup": RET2SHELL_IPC_SETUP,
}


class Ret2ShellWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    game_id: int = Field(gt=0)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    ends_at: str = ""
    remote_concurrency: int = Field(default=0, ge=0, le=100)
    category_map: dict[str, str] = Field(default_factory=dict)
    allow_private_networks: bool = False
    max_attachment_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024
    )
    secrets: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)

    @field_validator("category_map")
    @classmethod
    def validate_category_map(cls, value):
        if any(
            not key or len(key) > 256 or category not in CATEGORIES
            for key, category in value.items()
        ):
            raise ValueError(
                "category_map must map challenge IDs, category names or * to a "
                "supported category"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = _validate_http_url(value).rstrip("/")
        parsed = urlsplit(value)
        if parsed.query or parsed.path not in ("", "/"):
            raise ValueError(
                "base_url must be the ret2shell platform origin, for example "
                "https://ctf.example"
            )
        return value

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {
            "ret2shell_token",
            "ret2shell_username",
            "ret2shell_password",
            "platform_token",
        }
        if set(values) - allowed:
            raise ValueError(
                "ret2shell credentials must use ret2shell_token or "
                "ret2shell_username/ret2shell_password"
            )
        if any(not value.strip() or len(value) > 16384 for value in values.values()):
            raise ValueError("credentials must contain 1 to 16384 characters")
        return values

    def workflow_spec(self) -> PlatformWorkflowSpec:
        return PlatformWorkflowSpec.model_validate(
            {
                "name": self.name.strip() or f"ret2shell Game {self.game_id}",
                "competition_id": str(self.game_id),
                "team_id": self.team_id.strip() or "ret2shell",
                "template_id": "ret2shell",
                "template_version": RET2SHELL_TEMPLATE_VERSION,
                "ends_at": self.ends_at,
                "remote_concurrency": self.remote_concurrency,
                "allow_private_networks": self.allow_private_networks,
                "max_attachment_bytes": self.max_attachment_bytes,
                "challenges": {
                    "platform": "ret2shell",
                    "game_id": self.game_id,
                    "list_url": self.base_url,
                    "category_map": self.category_map,
                },
            }
        )


WORKFLOW_INPUT = Ret2ShellWorkflowInput


def configure(
    service: Any, data: dict[str, Any], *, workflow_id: str | None = None
) -> dict[str, Any]:
    """Create or update the ret2shell workflow through the ops service."""

    settings = Ret2ShellWorkflowInput.model_validate(data)
    spec = settings.workflow_spec()
    if workflow_id:
        existing = service.store.get_workflow(workflow_id)
        if existing["spec"].challenges.platform != PLATFORM:
            raise ValueError(
                "only a ret2shell workflow can be updated with the ret2shell template"
            )
        return service.update_workflow(
            workflow_id, spec.model_dump(), secrets_values=settings.secrets
        )
    return service.create_workflow(spec.model_dump(), secrets_values=settings.secrets)


def build_deployment(environ: Any = None) -> dict[str, Any] | None:
    """Build the deployment client and Member MCP from environment defaults.

    The env vars are optional operational configuration, not platform
    defaults: without all of them no Member MCP is registered and a ret2shell
    competition is expected to be configured through an IPC workflow.
    """

    import logging
    import os

    env = os.environ if environ is None else environ
    base_url = str(env.get("IPC_R2S_BASE_URL", "") or "").strip()
    game_id = str(env.get("IPC_R2S_GAME_ID", "") or "").strip()
    username = str(env.get("IPC_R2S_USERNAME", "") or "").strip()
    password = str(env.get("IPC_R2S_PASSWORD", "") or "")
    token = str(env.get("IPC_R2S_TOKEN", "") or "").strip()
    if not (base_url and game_id and (username or token)):
        if base_url or game_id or username or token:
            logging.getLogger(__name__).warning(
                "ret2shell deployment env is incomplete; the Member MCP stays "
                "disabled. Set IPC_R2S_BASE_URL, IPC_R2S_GAME_ID and "
                "IPC_R2S_USERNAME/IPC_R2S_TOKEN, or configure ret2shell through "
                "an IPC workflow."
            )
        return None

    from backend.platform.ret2shell import Ret2ShellClient
    from backend.platform.ret2shell.mcp import build_ret2shell_mcp

    client = Ret2ShellClient(
        base_url=base_url,
        game_id=game_id,
        username=username,
        password=password,
        token=token,
    )
    return {
        "platform": PLATFORM,
        "client": client,
        "mcp": build_ret2shell_mcp(client),
    }


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "MCP_NAME",
    "MCP_TOOL_DESCRIPTION",
    "PLATFORM",
    "RET2SHELL_IPC_SETUP",
    "RET2SHELL_PERSONALIZATION",
    "RET2SHELL_TEMPLATE_VERSION",
    "RUNTIME_MCP_NOTE",
    "TEMPLATE",
    "TOOL_DEFINITION",
    "TOOL_NAME",
    "WORKFLOW_INPUT",
    "Ret2ShellWorkflowInput",
    "build_deployment",
    "configure",
    "validate_mapping",
]
