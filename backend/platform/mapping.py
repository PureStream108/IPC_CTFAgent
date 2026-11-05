from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = "http_json"
    game_id: int | None = None
    list_url: str = ""
    list_path: str = "data"
    id_field: str = "id"
    title_field: str = "name"
    category_field: str = "category"
    description_field: str = "description"
    attachments_field: str = "files"
    remote_field: str = ""
    solved_field: str = ""
    score_field: str = "score"
    hints_field: str = ""
    level: int = 1
    track_id: str = ""
    level_field: str = ""
    track_id_field: str = ""
    pagination_path: str = ""
    max_pages: int = 100
    max_challenges: int = 0
    allow_hints: bool = False
    attachments_only: bool = False
    exclude: list[str] = Field(default_factory=list)
    category_map: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    attachment_base_url: str = ""

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        from backend.platform.presets import is_known_platform

        if not is_known_platform(value):
            raise ValueError(f"unsupported platform: {value}")
        return value

    @field_validator("list_url")
    @classmethod
    def validate_list_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("list_url must use http or https")
        return value

    @model_validator(mode="after")
    def require_list_url_for_http_json(self) -> FieldMapping:
        if self.platform == "http_json" and not self.list_url:
            raise ValueError("list_url is required for the http_json platform")
        # The platform package owns its identity rules; see
        # backend.platform.presets for the registry.
        from backend.platform.presets import validate_platform_mapping

        validate_platform_mapping(self)
        return self

    @field_validator("level")
    @classmethod
    def validate_level(cls, value: int) -> int:
        if value < 1:
            raise ValueError("level must be positive")
        return value

    @field_validator("max_pages")
    @classmethod
    def validate_max_pages(cls, value: int) -> int:
        if value < 1 or value > 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        return value

    @field_validator("max_challenges")
    @classmethod
    def validate_max_challenges(cls, value: int) -> int:
        if value < 0 or value > 1000:
            raise ValueError("max_challenges must be between 0 and 1000")
        return value

    @field_validator("level_field", "track_id_field", "pagination_path", "score_field")
    @classmethod
    def validate_optional_paths(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 256:
            raise ValueError("mapping paths are limited to 256 characters")
        return value


class PlatformChallenge(BaseModel):
    external_id: str
    title: str
    category: str
    description: str
    attachment_urls: list[str] = Field(default_factory=list)
    remote: bool = False
    solved: bool = False
    hints: list[str] = Field(default_factory=list)
    platform_data: dict[str, object] = Field(default_factory=dict)
