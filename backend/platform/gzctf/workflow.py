from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

GZCTF_TEMPLATE_VERSION = 1
TEMPLATE = {'id': 'gzctf', 'name': 'GZCTF', 'version': 1, 'default': True, 'required': ['base_url', 'game_id'], 'optional': ['name', 'team_id', 'ends_at', 'remote_concurrency', 'attachment_base_url', 'allow_private_networks', 'category_map'], 'secret_names': ['gzctf_token', 'gzctf_username', 'gzctf_password'], 'capabilities': ['challenges', 'attachments', 'submit', 'query', 'instances']}


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
                           "list_url": self.base_url, "attachment_base_url": self.attachment_base_url, "category_map": self.category_map},
        })


