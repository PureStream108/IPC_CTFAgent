from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.config import CATEGORIES
from backend.ops.models import PlatformWorkflowSpec, _validate_http_url

TSECBENCH_TEMPLATE_VERSION = 1
TEMPLATE = {'id': 'tsecbench', 'name': 'TSecBench', 'version': 1, 'default': False, 'required': ['base_url'], 'optional': ['name', 'team_id', 'remote_concurrency', 'allow_private_networks', 'category_map', 'allow_hints'], 'secret_names': ['benchmark_token'], 'capabilities': ['challenges', 'submit', 'instances', 'hint']}


class TSECBenchWorkflowInput(BaseModel):
    """Personalization for one benchmark task; identity comes from the token.

    The task has no ids of its own, so ``competition_id`` and ``team_id`` get
    stable operational defaults that keep run identity keys deterministic;
    the token in the secret store is what actually scopes the challenge set.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    name: str = Field(default="", max_length=120)
    team_id: str = Field(default="", max_length=256)
    remote_concurrency: int = Field(default=0, ge=0, le=3)
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
            "remote_concurrency": self.remote_concurrency,
            "allow_private_networks": self.allow_private_networks,
            "challenges": {"platform": "tsecbench", "list_url": self.base_url, "category_map": self.category_map, "allow_hints": self.allow_hints},
        })
