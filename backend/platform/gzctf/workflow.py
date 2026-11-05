from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

GZCTF_TEMPLATE_VERSION = 1
GZCTF_PERSONALIZATION = [
    {
        "key": "base_url",
        "label": "GZCTF platform URL",
        "kind": "url",
        "required": True,
        "help": "Scheme and host only, for example https://ctf.example",
    },
    {
        "key": "game_id",
        "label": "GZCTF game ID",
        "kind": "integer",
        "required": True,
        "help": "The numeric game id from the GZCTF game URL or admin page",
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
        "key": "attachment_base_url",
        "label": "Attachment CDN URL",
        "kind": "url",
        "required": False,
    },
    {
        "key": "allow_private_networks",
        "label": "Allow private platform network",
        "kind": "boolean",
        "required": False,
    },
    {
        "key": "category_map",
        "label": "Category overrides",
        "kind": "object",
        "required": False,
    },
]
GZCTF_IPC_SETUP = {
    "tool": "gzctf_workflow",
    "summary": "Connect a GZCTF game and use the native challenge, attachment, submit and instance APIs.",
    "prompt": (
        "Set up the built-in GZCTF preset workflow. Use the gzctf_workflow tool, not "
        "propose_platform_workflow. Ask me for the GZCTF platform URL and numeric game ID "
        "first; create the draft as soon as those identity fields are known, then ask for "
        "authentication. Ask one missing non-secret field at a time with the question tool. "
        "Never ask for a token or password in ordinary chat: after the draft exists, ask "
        "with the question tool, workflow_id and secret_name=gzctf_token "
        "(or gzctf_username/gzctf_password). Do not invent values or probe the platform "
        "before I confirm the draft."
    ),
    "personalization": GZCTF_PERSONALIZATION,
}
PLATFORM = "gzctf"
TOOL_NAME = "gzctf_workflow"
ALLOWED_SECRET_NAMES = frozenset(
    {"gzctf_token", "gzctf_username", "gzctf_password", "platform_token"}
)
TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Create or update the built-in GZCTF preset workflow. Use this instead of "
        "propose_platform_workflow for GZCTF. Supply only non-secret settings; base_url "
        "and game_id are required. Ask the operator for missing values with the question "
        "tool, one field at a time. After the draft exists, ask for credentials with "
        "question, workflow_id and secret_name=gzctf_token (or gzctf_username/"
        "gzctf_password). Never put credentials in settings or ordinary chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": "GZCTFWorkflowInput fields without secrets.",
                "additionalProperties": True,
            },
            "workflow_id": {"type": "string"},
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
}
MCP_TOOL_DESCRIPTION = (
    "Create or update the built-in default GZCTF workflow. Supply only platform base_url, "
    "game_id and optional name/team_id/ends_at/remote_concurrency/attachment_base_url. "
    "No HTTP mappings or platform probing are needed. Ask ipc_question for any missing "
    "base_url or game_id; after creating the draft, ask for gzctf_token (or "
    "gzctf_username/gzctf_password) with ipc_question. Return the draft for normal "
    "confirmation."
)


def validate_mapping(mapping: Any) -> None:
    if mapping.game_id is None:
        raise ValueError("game_id is required for the gzctf platform")


TEMPLATE = {
    "id": "gzctf",
    "name": "GZCTF",
    "version": 1,
    "default": True,
    "required": ["base_url", "game_id"],
    "optional": [
        "name",
        "team_id",
        "ends_at",
        "remote_concurrency",
        "attachment_base_url",
        "allow_private_networks",
        "category_map",
    ],
    "secret_names": ["gzctf_token", "gzctf_username", "gzctf_password"],
    "capabilities": ["challenges", "attachments", "submit", "query", "instances"],
    "ipc_setup": GZCTF_IPC_SETUP,
}


class GZCTFWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    game_id: int = Field(gt=0)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    ends_at: str = ""
    remote_concurrency: int = Field(default=0, ge=0, le=100)
    attachment_base_url: str = ""
    category_map: dict[str, str] = Field(default_factory=dict)
    allow_private_networks: bool = False
    max_attachment_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    secrets: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)

    @field_validator("category_map")
    @classmethod
    def validate_category_map(cls, value):
        if any(not key or len(key) > 256 or category not in CATEGORIES for key, category in value.items()):
            raise ValueError("category_map must map challenge IDs, category names or * to a supported category")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = _validate_http_url(value).rstrip("/")
        parsed = urlsplit(value)
        if parsed.query or parsed.path not in ("", "/"):
            raise ValueError("base_url must be the GZCTF platform origin, for example https://ctf.example")
        return value

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {"gzctf_token", "gzctf_username", "gzctf_password", "platform_token"}
        if set(values) - allowed:
            raise ValueError("GZCTF credentials must use gzctf_token or gzctf_username/gzctf_password")
        if any(not value.strip() or len(value) > 16384 for value in values.values()):
            raise ValueError("credentials must contain 1 to 16384 characters")
        return values

    def workflow_spec(self) -> PlatformWorkflowSpec:
        return PlatformWorkflowSpec.model_validate({
            "name": self.name.strip() or f"GZCTF Game {self.game_id}",
            "competition_id": str(self.game_id), "team_id": self.team_id,
            "template_id": "gzctf", "template_version": GZCTF_TEMPLATE_VERSION,
            "ends_at": self.ends_at, "remote_concurrency": self.remote_concurrency,
            "allow_private_networks": self.allow_private_networks,
            "max_attachment_bytes": self.max_attachment_bytes,
            "challenges": {"platform": "gzctf", "game_id": self.game_id,
                           "list_url": self.base_url, "attachment_base_url": self.attachment_base_url,
                           "category_map": self.category_map},
        })


WORKFLOW_INPUT = GZCTFWorkflowInput


def configure(
    service: Any, data: dict[str, Any], *, workflow_id: str | None = None
) -> dict[str, Any]:
    """Create or update the GZCTF workflow through the ops service."""

    settings = GZCTFWorkflowInput.model_validate(data)
    spec = settings.workflow_spec()
    if workflow_id:
        existing = service.store.get_workflow(workflow_id)
        if existing["spec"].challenges.platform != PLATFORM:
            raise ValueError(
                "only a GZCTF workflow can be updated with the GZCTF template"
            )
        return service.update_workflow(
            workflow_id, spec.model_dump(), secrets_values=settings.secrets
        )
    return service.create_workflow(spec.model_dump(), secrets_values=settings.secrets)


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "GZCTF_IPC_SETUP",
    "GZCTF_PERSONALIZATION",
    "GZCTF_TEMPLATE_VERSION",
    "MCP_TOOL_DESCRIPTION",
    "PLATFORM",
    "TEMPLATE",
    "TOOL_DEFINITION",
    "TOOL_NAME",
    "WORKFLOW_INPUT",
    "GZCTFWorkflowInput",
    "configure",
    "validate_mapping",
]
