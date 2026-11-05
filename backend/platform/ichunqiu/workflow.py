from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

ICHUNQIU_TEMPLATE_VERSION = 1
ICHUNQIU_PERSONALIZATION = [
    {
        "key": "base_url",
        "label": "i春秋 match URL",
        "kind": "url",
        "required": True,
        "help": "Scheme and host only, for example https://match.ichunqiu.com",
    },
    {
        "key": "url_key",
        "label": "Match slug",
        "kind": "text",
        "required": True,
        "help": "The slug from the match URL, for example 2026qwbs10",
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
        "key": "attachments_only",
        "label": "Import attachment challenges only",
        "kind": "boolean",
        "required": False,
        "help": (
            "Skip challenges that need a per-team container and import only "
            "challenges solved from a downloaded attachment."
        ),
    },
    {
        "key": "exclude",
        "label": "Excluded challenges",
        "kind": "list",
        "required": False,
        "help": (
            "Challenge ids or titles the run must not import, for example a task "
            "whose attachment is too large to work on."
        ),
    },
    {
        "key": "allow_private_networks",
        "label": "Allow private platform network",
        "kind": "boolean",
        "required": False,
    },
]
ICHUNQIU_IPC_SETUP = {
    "tool": "ichunqiu_workflow",
    "summary": (
        "Connect an i春秋 match (match.ichunqiu.com) and use the native challenge, "
        "attachment, submit and container APIs."
    ),
    "prompt": (
        "Set up the built-in i春秋 preset workflow. Use the ichunqiu_workflow tool, not "
        "propose_platform_workflow. Ask me for the i春秋 match URL and its slug "
        "(for example 2026qwbs10) first; create the draft as soon as those identity "
        "fields are known, then ask for authentication. Ask one missing non-secret field "
        "at a time with the question tool. Never ask for a token in ordinary chat: after "
        "the draft exists, ask with the question tool, workflow_id and "
        "secret_name=ichunqiu_token. The token comes from a logged-in browser session; "
        "run python -m backend.platform.ichunqiu.browser_setup to capture it."
    ),
    "personalization": ICHUNQIU_PERSONALIZATION,
}
PLATFORM = "ichunqiu"
TOOL_NAME = "ichunqiu_workflow"
ALLOWED_SECRET_NAMES = frozenset({"ichunqiu_token", "platform_token"})
TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Create or update the built-in i春秋 preset workflow. Use this instead of "
        "propose_platform_workflow for match.ichunqiu.com. Supply only non-secret "
        "settings; base_url and url_key are required. Ask the operator for missing "
        "values with the question tool, one field at a time. After the draft exists, "
        "ask for credentials with question, workflow_id and "
        "secret_name=ichunqiu_token. Never put credentials in settings or ordinary chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": "IchunqiuWorkflowInput fields without secrets.",
                "additionalProperties": True,
            },
            "workflow_id": {"type": "string"},
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
}
MCP_TOOL_DESCRIPTION = (
    "Create or update the built-in i春秋 workflow. Supply only the platform base_url, "
    "the match slug (url_key) and optional name/team_id/ends_at/remote_concurrency. "
    "Ask ipc_question for any missing base_url or url_key; after creating the draft, ask "
    "for ichunqiu_token with ipc_question. Return the draft for normal confirmation."
)


def validate_mapping(mapping: Any) -> None:
    if not str(getattr(mapping, "list_url", "") or "").strip():
        raise ValueError("list_url is required for the ichunqiu platform")


TEMPLATE = {
    "id": "ichunqiu",
    "name": "i春秋",
    "version": 1,
    "default": False,
    "required": ["base_url", "url_key"],
    "optional": [
        "name",
        "team_id",
        "ends_at",
        "remote_concurrency",
        "category_map",
        "attachments_only",
        "exclude",
        "allow_private_networks",
    ],
    "secret_names": ["ichunqiu_token"],
    "capabilities": ["challenges", "attachments", "submit", "instances"],
    "ipc_setup": ICHUNQIU_IPC_SETUP,
}


class IchunqiuWorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="https://match.ichunqiu.com", max_length=2048)
    url_key: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    ends_at: str = ""
    remote_concurrency: int = Field(default=0, ge=0, le=100)
    category_map: dict[str, str] = Field(default_factory=dict)
    attachments_only: bool = False
    exclude: list[str] = Field(default_factory=list)
    allow_private_networks: bool = False
    max_attachment_bytes: int = Field(
        default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024
    )
    secrets: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)

    @field_validator("url_key")
    @classmethod
    def validate_url_key(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not value or "/" in value or "?" in value:
            raise ValueError("url_key must be the bare match slug, for example 2026qwbs10")
        return value

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
                "base_url must be the i春秋 origin, for example https://match.ichunqiu.com"
            )
        return value

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {"ichunqiu_token", "platform_token"}
        if set(values) - allowed:
            raise ValueError("i春秋 credentials must use ichunqiu_token")
        if any(not value.strip() or len(value) > 16384 for value in values.values()):
            raise ValueError("credentials must contain 1 to 16384 characters")
        return values

    def workflow_spec(self) -> PlatformWorkflowSpec:
        return PlatformWorkflowSpec.model_validate(
            {
                "name": self.name.strip() or f"i春秋 {self.url_key}",
                "competition_id": self.url_key,
                "team_id": self.team_id.strip() or "ichunqiu",
                "template_id": "ichunqiu",
                "template_version": ICHUNQIU_TEMPLATE_VERSION,
                "ends_at": self.ends_at,
                "remote_concurrency": self.remote_concurrency,
                "allow_private_networks": self.allow_private_networks,
                "max_attachment_bytes": self.max_attachment_bytes,
                "challenges": {
                    "platform": "ichunqiu",
                    "list_url": self.base_url,
                    "category_map": self.category_map,
                    "attachments_only": self.attachments_only,
                    "exclude": self.exclude,
                },
            }
        )


WORKFLOW_INPUT = IchunqiuWorkflowInput


def configure(
    service: Any, data: dict[str, Any], *, workflow_id: str | None = None
) -> dict[str, Any]:
    """Create or update the i春秋 workflow through the ops service."""

    settings = IchunqiuWorkflowInput.model_validate(data)
    spec = settings.workflow_spec()
    if workflow_id:
        existing = service.store.get_workflow(workflow_id)
        if existing["spec"].challenges.platform != PLATFORM:
            raise ValueError(
                "only an i春秋 workflow can be updated with the i春秋 template"
            )
        return service.update_workflow(
            workflow_id, spec.model_dump(), secrets_values=settings.secrets
        )
    return service.create_workflow(spec.model_dump(), secrets_values=settings.secrets)


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "ICHUNQIU_IPC_SETUP",
    "ICHUNQIU_PERSONALIZATION",
    "ICHUNQIU_TEMPLATE_VERSION",
    "MCP_TOOL_DESCRIPTION",
    "PLATFORM",
    "TEMPLATE",
    "TOOL_DEFINITION",
    "TOOL_NAME",
    "WORKFLOW_INPUT",
    "IchunqiuWorkflowInput",
    "configure",
    "validate_mapping",
]
