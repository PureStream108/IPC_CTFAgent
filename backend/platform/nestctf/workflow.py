"""nestctf (强网拟态) preset: one race, JWT, handout-only challenges."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

NESTCTF_TEMPLATE_VERSION = 1

NESTCTF_PERSONALIZATION = [
    {
        "key": "base_url",
        "label": "nestctf platform URL",
        "kind": "url",
        "required": True,
        "help": "Scheme and host only, for example https://nestctf.cyberpeace.cn:54443",
    },
    {
        "key": "race_id",
        "label": "Race id",
        "kind": "text",
        "required": True,
        "help": "The race id from the contest URL (/jeopardy_race/race/<id>/)",
    },
    {"key": "name", "label": "Competition name", "kind": "text", "required": False},
    {"key": "team_id", "label": "Team id or name", "kind": "text", "required": False},
    {"key": "ends_at", "label": "Deadline", "kind": "datetime", "required": False},
    {
        "key": "attachments_only",
        "label": "Only challenges that need no container",
        "kind": "boolean",
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

NESTCTF_IPC_SETUP = {
    "tool": "nestctf_workflow",
    "summary": (
        "Connect a nestctf race and use the native checkpoint, handout and submit APIs. "
        "with_scene splits container challenges from handout-only ones."
    ),
    "prompt": (
        "Set up the built-in nestctf preset workflow. Use the nestctf_workflow tool, "
        "not propose_platform_workflow. Ask me for the nestctf origin and the race id "
        "first, then create the draft. Ask for credentials only after the draft exists, "
        "with question, workflow_id and secret_name=nestctf_token (an optional "
        "nestctf_cookie covers the handout download host). Never put credentials in "
        "settings or ordinary chat."
    ),
    "personalization": NESTCTF_PERSONALIZATION,
}

PLATFORM = "nestctf"
TOOL_NAME = "nestctf_workflow"
ALLOWED_SECRET_NAMES = frozenset(
    {"nestctf_token", "nestctf_cookie", "platform_token"}
)

TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Create or update the built-in nestctf preset workflow. Use this instead of "
        "propose_platform_workflow for nestctf races. Supply only non-secret settings; "
        "base_url and race_id are required. Ask the operator for a missing value with "
        "the question tool, one field at a time, and ask for credentials only after the "
        "draft exists (secret_name=nestctf_token)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": "NestctfWorkflowInput fields without secrets.",
                "additionalProperties": True,
            },
            "workflow_id": {"type": "string"},
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
}

MCP_TOOL_DESCRIPTION = (
    "Create or update the built-in nestctf workflow. Supply the race origin, the race "
    "id and optional name/team_id/attachments_only. Ask ipc_question for a missing "
    "base_url or race_id, and for nestctf_token after the draft exists. Return the "
    "draft for normal confirmation."
)


def validate_mapping(mapping: Any) -> None:
    # The race id travels in competition_id, which FieldMapping does not carry;
    # only the origin can be checked here.
    if not mapping.list_url.strip():
        raise ValueError("list_url is required for the nestctf platform")


TEMPLATE = {
    "id": "nestctf",
    "name": "nestctf (强网拟态)",
    "version": NESTCTF_TEMPLATE_VERSION,
    "default": False,
    "required": ["base_url", "race_id"],
    "optional": [
        "name",
        "team_id",
        "ends_at",
        "attachments_only",
        "category_map",
        "allow_private_networks",
    ],
    "secret_names": ["nestctf_token", "nestctf_cookie"],
    "capabilities": ["challenges", "attachments", "submit"],
    "ipc_setup": NESTCTF_IPC_SETUP,
}


class NestctfWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    race_id: str = Field(min_length=8, max_length=128)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    ends_at: str = ""
    attachments_only: bool = True
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
        value = _validate_http_url(value)
        parsed = urlsplit(value)
        if parsed.query or parsed.path not in ("", "/"):
            raise ValueError(
                "base_url must be the platform origin, for example "
                "https://nestctf.cyberpeace.cn:54443"
            )
        return value.rstrip("/")

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        if set(values) - ALLOWED_SECRET_NAMES:
            raise ValueError(
                "nestctf credentials must use nestctf_token or nestctf_cookie"
            )
        if any(not value.strip() or len(value) > 16384 for value in values.values()):
            raise ValueError("credentials must contain 1 to 16384 characters")
        return values

    def workflow_spec(self) -> PlatformWorkflowSpec:
        race = self.race_id.strip()
        base = self.base_url.rstrip("/")
        race_url = base + "/api/ct/web/jeopardy_race/race/" + race
        return PlatformWorkflowSpec.model_validate(
            {
                "name": self.name.strip() or ("nestctf race " + race[:8]),
                "template_id": "nestctf",
                "template_version": NESTCTF_TEMPLATE_VERSION,
                "competition_id": race,
                "team_id": self.team_id.strip() or "nestctf",
                "ends_at": self.ends_at,
                "remote_concurrency": self.remote_concurrency,
                "allow_private_networks": self.allow_private_networks,
                "max_attachment_bytes": self.max_attachment_bytes,
                "challenges": {
                    "platform": "nestctf",
                    "list_url": base,
                    "game_id": None,
                    "attachments_only": self.attachments_only,
                    "category_map": self.category_map,
                    "headers": [
                        {
                            "name": "Authorization",
                            "prefix": "JWT ",
                            "secret_name": "nestctf_token",
                        }
                    ],
                },
                "submit": {
                    "url": race_url + "/flag/",
                    "method": "POST",
                    "headers": [
                        {
                            "name": "Authorization",
                            "prefix": "JWT ",
                            "secret_name": "nestctf_token",
                        }
                    ],
                    "json_template": {
                        "flag": "{{flag}}",
                        "checkpoint_id": "{{external_id}}",
                    },
                    "success_path": "data.is_pass",
                    "success_values": [True],
                    "success_statuses": [200, 201, 202, 204],
                },
            }
        )


WORKFLOW_INPUT = NestctfWorkflowInput


def configure(
    service: Any, data: dict[str, Any], *, workflow_id: str | None = None
) -> dict[str, Any]:
    """Create or update the nestctf workflow through the ops service."""

    settings = NestctfWorkflowInput.model_validate(data)
    spec = settings.workflow_spec()
    if workflow_id:
        existing = service.store.get_workflow(workflow_id)
        if existing["spec"].challenges.platform != PLATFORM:
            raise ValueError(
                "only a nestctf workflow can be updated with the nestctf template"
            )
        return service.update_workflow(
            workflow_id, spec.model_dump(), secrets_values=settings.secrets
        )
    return service.create_workflow(spec.model_dump(), secrets_values=settings.secrets)


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "MCP_TOOL_DESCRIPTION",
    "NESTCTF_IPC_SETUP",
    "NESTCTF_PERSONALIZATION",
    "NESTCTF_TEMPLATE_VERSION",
    "PLATFORM",
    "TEMPLATE",
    "TOOL_DEFINITION",
    "TOOL_NAME",
    "WORKFLOW_INPUT",
    "NestctfWorkflowInput",
    "configure",
    "validate_mapping",
]
