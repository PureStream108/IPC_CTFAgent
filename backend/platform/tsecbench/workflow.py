from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

TSECBENCH_TEMPLATE_VERSION = 1
TSECBENCH_PERSONALIZATION = [
    {
        "key": "base_url",
        "label": "TSecBench platform URL",
        "kind": "url",
        "required": True,
        "help": "Scheme and host only, for example https://tsecbench.example.com",
    },
    {"key": "name", "label": "Benchmark name", "kind": "text", "required": False},
    {"key": "team_id", "label": "Team id or name", "kind": "text", "required": False},
    {
        "key": "remote_concurrency",
        "label": "Remote container concurrency",
        "kind": "integer",
        "required": False,
        "help": "0 uses the platform default; the maximum is 3",
    },
    {
        "key": "duration_minutes",
        "label": "Local deadline guard (minutes, optional)",
        "kind": "integer",
        "required": False,
        "help": (
            "Leave 0 (default) to let the competition end or the platform task "
            "window stop the run. A positive value is only a local guard and is "
            "promoted to the declared window when it would end earlier."
        ),
    },
    {
        "key": "allow_private_networks",
        "label": "Allow configured private network",
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
        "key": "allow_hints",
        "label": "Allow hints that reduce the score",
        "kind": "boolean",
        "required": False,
    },
]
TSECBENCH_IPC_SETUP = {
    "tool": "tsecbench_workflow",
    "summary": "Connect a TSecBench evaluation, import its tasks and use the native submit and instance APIs.",
    "prompt": (
        "Set up the built-in TSecBench preset workflow. Use the tsecbench_workflow tool, "
        "not propose_platform_workflow. Ask me for the TSecBench platform URL first; create "
        "the draft as soon as it is known, then ask for authentication. Ask one missing "
        "non-secret field at a time with the question tool. Never ask for the benchmark "
        "token in ordinary chat: after the draft exists, ask with the question tool, "
        "workflow_id and secret_name=benchmark_token. Do not invent values or probe the "
        "platform before I confirm the draft."
    ),
    "personalization": TSECBENCH_PERSONALIZATION,
}
PLATFORM = "tsecbench"
TOOL_NAME = "tsecbench_workflow"
ALLOWED_SECRET_NAMES = frozenset({"benchmark_token", "platform_token"})
TOOL_DEFINITION = {
    "name": TOOL_NAME,
    "description": (
        "Create or update the built-in TSecBench preset workflow. Use this instead of "
        "propose_platform_workflow for TSecBench. Supply only non-secret settings; "
        "base_url is required. Ask the operator for missing values with the question "
        "tool, one field at a time. After the draft exists, ask for the benchmark token "
        "with question, workflow_id and secret_name=benchmark_token. Never put "
        "credentials in settings or ordinary chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "settings": {
                "type": "object",
                "description": "TSECBenchWorkflowInput fields without secrets.",
                "additionalProperties": True,
            },
            "workflow_id": {"type": "string"},
        },
        "required": ["settings"],
        "additionalProperties": False,
    },
}
MCP_TOOL_DESCRIPTION = (
    "Create or update the built-in default TSecBench benchmark workflow. Supply only "
    "the platform base_url and optional name/team_id/remote_concurrency. No HTTP "
    "mappings or platform probing are needed. The benchmark token goes into the "
    "workflow secret store as benchmark_token; ask ipc_question with "
    "secret_name=benchmark_token after creating the draft when it is missing. "
    "Return the draft for normal confirmation."
)


def validate_mapping(mapping: Any) -> None:
    if not mapping.list_url.strip():
        raise ValueError("list_url is required for the tsecbench platform")


TEMPLATE = {
    "id": "tsecbench",
    "name": "TSecBench",
    "version": 1,
    "default": False,
    "required": ["base_url"],
    "optional": [
        "name",
        "team_id",
        "duration_minutes",
        "remote_concurrency",
        "allow_private_networks",
        "category_map",
        "allow_hints",
    ],
    "secret_names": ["benchmark_token"],
    "capabilities": ["challenges", "submit", "instances", "hint"],
    "ipc_setup": TSECBENCH_IPC_SETUP,
}


class TSECBenchWorkflowInput(BaseModel):
    """Personalization for one benchmark task; identity comes from the token.

    The task has no ids of its own, so competition_id and team_id get stable
    operational defaults that keep run identity keys deterministic; the token
    in the secret store is what actually scopes the challenge set.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    remote_concurrency: int = Field(default=0, ge=0, le=3)
    duration_minutes: int = Field(default=0, ge=0, le=1440)
    allow_hints: bool = False
    category_map: dict[str, str] = Field(default_factory=dict)
    allow_private_networks: bool = False
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
            raise ValueError("base_url must be the TSecBench platform origin, for example https://tsecbench.example.com")
        return value

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, values: dict[str, str]) -> dict[str, str]:
        allowed = {"benchmark_token", "platform_token"}
        if set(values) - allowed:
            raise ValueError("TSecBench credentials must use benchmark_token")
        if any(not value.strip() or len(value) > 16384 for value in values.values()):
            raise ValueError("credentials must contain 1 to 16384 characters")
        return values

    def workflow_spec(self) -> PlatformWorkflowSpec:
        return PlatformWorkflowSpec.model_validate({
            "name": self.name.strip() or "TSecBench Benchmark",
            "competition_id": "tsecbench", "team_id": self.team_id.strip() or "benchmark",
            "template_id": "tsecbench", "template_version": TSECBENCH_TEMPLATE_VERSION,
            "duration_minutes": self.duration_minutes,
            "remote_concurrency": self.remote_concurrency,
            "allow_private_networks": self.allow_private_networks,
            "challenges": {"platform": "tsecbench", "list_url": self.base_url,
                           "category_map": self.category_map, "allow_hints": self.allow_hints},
        })


WORKFLOW_INPUT = TSECBenchWorkflowInput


def configure(
    service: Any, data: dict[str, Any], *, workflow_id: str | None = None
) -> dict[str, Any]:
    """Create or update the TSecBench workflow through the ops service."""

    settings = TSECBenchWorkflowInput.model_validate(data)
    spec = settings.workflow_spec()
    if workflow_id:
        existing = service.store.get_workflow(workflow_id)
        if existing["spec"].challenges.platform != PLATFORM:
            raise ValueError(
                "only a TSecBench workflow can be updated with the TSecBench template"
            )
        return service.update_workflow(
            workflow_id, spec.model_dump(), secrets_values=settings.secrets
        )
    return service.create_workflow(spec.model_dump(), secrets_values=settings.secrets)


__all__ = [
    "ALLOWED_SECRET_NAMES",
    "MCP_TOOL_DESCRIPTION",
    "PLATFORM",
    "TEMPLATE",
    "TOOL_DEFINITION",
    "TOOL_NAME",
    "TSECBENCH_IPC_SETUP",
    "TSECBENCH_PERSONALIZATION",
    "TSECBENCH_TEMPLATE_VERSION",
    "WORKFLOW_INPUT",
    "TSECBenchWorkflowInput",
    "configure",
    "validate_mapping",
]
