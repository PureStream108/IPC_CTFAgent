from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.platform.mapping import FieldMapping

_SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PLACEHOLDER_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")


def validate_secret_name(value: str) -> str:
    value = value.strip().lower()
    if not _SECRET_NAME_RE.fullmatch(value):
        raise ValueError("secret names must match [a-z][a-z0-9_]{0,63}")
    return value


def _validate_http_url(value: str, *, allow_external_id: bool = False) -> str:
    value = value.strip()
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("workflow URLs cannot contain whitespace or control characters")
    candidate = value.replace("{{external_id}}", "challenge-id") if allow_external_id else value
    if "{{" in candidate or "}}" in candidate:
        raise ValueError("URL contains an unsupported template placeholder")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("credentials are not allowed in workflow URLs")
    if parsed.fragment:
        raise ValueError("fragments are not allowed in workflow URLs")
    sensitive_query_names = {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "key",
        "password",
        "secret",
        "token",
    }
    for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if name.strip().lower() in sensitive_query_names:
            raise ValueError("credentials are not allowed in workflow URL query parameters")
    return value


class SecretHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    secret_name: str
    prefix: str = Field(default="", max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _HEADER_NAME_RE.fullmatch(value):
            raise ValueError("invalid HTTP header name")
        return value

    @field_validator("secret_name")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        return validate_secret_name(value)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("header prefixes cannot contain newlines")
        return value


class ChallengeMappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    list_url: str
    list_path: str = "data"
    id_field: str = "id"
    title_field: str = "name"
    category_field: str = "category"
    description_field: str = "description"
    attachments_field: str = "files"
    category_map: dict[str, str] = Field(default_factory=dict)
    attachment_base_url: str = ""
    headers: list[SecretHeader] = Field(default_factory=list)

    @field_validator("list_url")
    @classmethod
    def validate_list_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("attachment_base_url")
    @classmethod
    def validate_attachment_base_url(cls, value: str) -> str:
        return _validate_http_url(value) if value.strip() else ""

    @field_validator(
        "list_path",
        "id_field",
        "title_field",
        "category_field",
        "description_field",
        "attachments_field",
    )
    @classmethod
    def validate_json_path(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 256:
            raise ValueError("JSON paths are limited to 256 characters")
        return value

    @model_validator(mode="after")
    def unique_headers(self) -> ChallengeMappingSpec:
        names = [header.name.lower() for header in self.headers]
        if len(names) != len(set(names)):
            raise ValueError("challenge header names must be unique")
        return self

    def to_field_mapping(self, headers: dict[str, str]) -> FieldMapping:
        return FieldMapping(
            list_url=self.list_url,
            list_path=self.list_path,
            id_field=self.id_field,
            title_field=self.title_field,
            category_field=self.category_field,
            description_field=self.description_field,
            attachments_field=self.attachments_field,
            category_map=self.category_map,
            headers=headers,
            attachment_base_url=self.attachment_base_url,
        )


class FlagSubmitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["POST", "PUT"] = "POST"
    headers: list[SecretHeader] = Field(default_factory=list)
    json_template: dict[str, Any] = Field(
        default_factory=lambda: {
            "challenge_id": "{{external_id}}",
            "flag": "{{flag}}",
        }
    )
    success_statuses: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])
    success_path: str = ""
    success_values: list[Any] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_http_url(value, allow_external_id=True)

    @field_validator("success_statuses")
    @classmethod
    def validate_statuses(cls, values: list[int]) -> list[int]:
        if not values or len(values) > 20:
            raise ValueError("success_statuses must contain between 1 and 20 values")
        if any(value < 100 or value > 599 for value in values):
            raise ValueError("success_statuses contains an invalid HTTP status")
        return sorted(set(values))

    @model_validator(mode="after")
    def validate_templates(self) -> FlagSubmitSpec:
        names = [header.name.lower() for header in self.headers]
        if len(names) != len(set(names)):
            raise ValueError("submit header names must be unique")
        _validate_template_value(self.json_template)
        return self


class PlatformWorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    challenges: ChallengeMappingSpec
    submit: FlagSubmitSpec | None = None
    allow_private_networks: bool = False
    max_attachment_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return value.strip()

    def required_secret_names(self) -> set[str]:
        names = {header.secret_name for header in self.challenges.headers}
        if self.submit is not None:
            names.update(header.secret_name for header in self.submit.headers)
            names.update(_template_secret_names(self.submit.json_template))
        return names


def _template_secret_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            names.update(_template_secret_names(key))
            names.update(_template_secret_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_template_secret_names(item))
    elif isinstance(value, str):
        for placeholder in _PLACEHOLDER_RE.findall(value):
            if placeholder.startswith("secret."):
                names.add(validate_secret_name(placeholder.removeprefix("secret.")))
    return names


def _validate_template_value(value: Any, *, sensitive_field: bool = False) -> None:
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("JSON templates are limited to 100 keys per object")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ValueError("JSON template keys must be short strings")
            _validate_template_value(key)
            _validate_template_value(item, sensitive_field=_sensitive_template_key(key))
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("JSON template arrays are limited to 100 items")
        for item in value:
            _validate_template_value(item, sensitive_field=sensitive_field)
        return
    if isinstance(value, str):
        if len(value) > 10_000:
            raise ValueError("JSON template strings are limited to 10000 characters")
        placeholders = _PLACEHOLDER_RE.findall(value)
        if ("{{" in value or "}}" in value) and not placeholders:
            raise ValueError("malformed JSON template placeholder")
        for placeholder in placeholders:
            if placeholder in {"flag", "external_id"}:
                continue
            if placeholder.startswith("secret."):
                validate_secret_name(placeholder.removeprefix("secret."))
                continue
            raise ValueError(f"unsupported JSON template placeholder: {placeholder}")
        if sensitive_field and not any(
            placeholder.startswith("secret.") for placeholder in placeholders
        ):
            raise ValueError("credential-like JSON fields must use a structured secret placeholder")
        return
    if sensitive_field:
        raise ValueError("credential-like JSON fields must use a structured secret placeholder")
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError(f"unsupported JSON template value: {type(value).__name__}")


def _sensitive_template_key(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return (
        normalized
        in {
            "access_token",
            "api_key",
            "apikey",
            "auth",
            "authorization",
            "credential",
            "key",
            "password",
            "secret",
            "token",
        }
        or normalized.endswith(("_api_key", "_credential", "_password", "_secret", "_token"))
    )


def render_template(value: Any, *, external_id: str, flag: str, secrets: dict[str, str]) -> Any:
    replacements = {
        "external_id": external_id,
        "flag": flag,
        **{f"secret.{name}": secret for name, secret in secrets.items()},
    }
    if isinstance(value, dict):
        return {
            render_template(key, external_id=external_id, flag=flag, secrets=secrets): render_template(
                item,
                external_id=external_id,
                flag=flag,
                secrets=secrets,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            render_template(item, external_id=external_id, flag=flag, secrets=secrets)
            for item in value
        ]
    if not isinstance(value, str):
        return value
    exact = _PLACEHOLDER_RE.fullmatch(value)
    if exact:
        return replacements[exact.group(1)]

    def replace(match: re.Match[str]) -> str:
        return str(replacements[match.group(1)])

    return _PLACEHOLDER_RE.sub(replace, value)
