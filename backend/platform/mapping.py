from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``http_json`` fetches any JSON list endpoint with static headers.
    # ``ret2shell`` and ``gzctf`` use authenticated clients (credentials come
    # from deployment environment variables, never from a workflow snapshot).
    platform: Literal["http_json", "ret2shell", "gzctf"] = "http_json"
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
    hints_field: str = ""
    # GZCTF submissions are level based.  These values are deliberately
    # metadata rather than credentials and are safe to persist in a run
    # snapshot.  ``*_field`` permits a challenge list fixture to override the
    # defaults per challenge.
    level: int = 1
    track_id: str = ""
    level_field: str = ""
    track_id_field: str = ""
    # Optional cursor pagination for list endpoints.  The configured path
    # must resolve to the next URL (or null); an empty path keeps the original
    # single-request behavior.
    pagination_path: str = ""
    max_pages: int = 100
    category_map: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    attachment_base_url: str = ""

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
        if self.platform == "gzctf" and self.game_id is None:
            raise ValueError("game_id is required for the gzctf platform")
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

    @field_validator("level_field", "track_id_field", "pagination_path")
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
    # Adapter-specific, non-secret state (for example a GZCTF track/level or
    # attachment descriptor).  Competition snapshots only copy the selected
    # public fields, so this never becomes a credential transport.
    platform_data: dict[str, object] = Field(default_factory=dict)
