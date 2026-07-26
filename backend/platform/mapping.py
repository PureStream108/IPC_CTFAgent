from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_url: str
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
        if not value.startswith(("http://", "https://")):
            raise ValueError("list_url must use http or https")
        return value


class PlatformChallenge(BaseModel):
    external_id: str
    title: str
    category: str
    description: str
    attachment_urls: list[str] = Field(default_factory=list)
