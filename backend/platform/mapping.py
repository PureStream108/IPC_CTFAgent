from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``http_json`` fetches any JSON list endpoint with static headers.
    # ``ret2shell`` uses the authenticated ret2shell client (credentials come
    # from IPC_R2S_* environment variables, never from the request body).
    platform: Literal["http_json", "ret2shell"] = "http_json"
    game_id: int | None = None
    list_url: str = ""
    list_path: str = "data"
    id_field: str = "id"
    title_field: str = "name"
    category_field: str = "category"
    description_field: str = "description"
    attachments_field: str = "files"
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
        return self


class PlatformChallenge(BaseModel):
    external_id: str
    title: str
    category: str
    description: str
    attachment_urls: list[str] = Field(default_factory=list)
