from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# CTF challenge categories the user may pick when creating a project.
CATEGORIES: tuple[str, ...] = ("pwn", "reverse", "crypto", "web", "misc", "ai", "osint")

# Supported LLM wire formats. base_url is always user-provided.
# ``anthropic`` is the raw Messages API; ``claudecode`` keeps the separate
# Claude Code action runtime used by IPC.
ApiFormat = Literal["openai", "anthropic", "claudecode", "deepseek", "pi", "mock"]
ApiSurface = Literal["auto", "chat_completions", "responses"]
ReasoningEffort = Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]

# Default member names. These are worker identities, not different roles.
MEMBER_NAMES: tuple[str, ...] = ("aventurine", "pearl", "jade", "topaz")
MEMBER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class LLMConfig(BaseModel):
    """Per-agent LLM endpoint config (Diamond or a Member)."""

    model_config = ConfigDict(extra="forbid")

    api_format: ApiFormat = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    # OpenAI-compatible providers vary in which generation endpoint they
    # implement.  ``auto`` negotiates and caches a working endpoint, while the
    # explicit values are useful for strict gateways and future model families.
    api_surface: ApiSurface = "auto"
    # Kept provider-neutral in configuration.  Adapters translate this to
    # ``reasoning.effort`` (Responses) or ``reasoning_effort`` (Chat).
    reasoning_effort: ReasoningEffort = "auto"

    @property
    def configured(self) -> bool:
        """A mock agent needs no creds; everyone else needs key + base_url."""
        if self.api_format == "mock":
            return True
        return bool(self.api_key.strip()) and bool(self.base_url.strip())


class MemberConfig(LLMConfig):
    name: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        name = value.strip().lower()
        if name and not MEMBER_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                "member name must start with a letter and contain only lowercase letters, "
                "numbers, underscores, or hyphens (max 32 characters)"
            )
        return name


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cpu: int = 4
    # Max CTF tasks running concurrently. Each task owns one shared container;
    # containers are not memory-capped, so there is no per-agent memory limit.
    max_concurrent_tasks: int = Field(default=5, gt=0)
    network: bool = True


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Scheduler tick + heartbeat cadence (seconds).
    interval: int = Field(default=2, gt=0)
    intent_timeout: int = Field(default=30, ge=5)
    reason_timeout: int = Field(default=30, ge=5)
    # Max extra Members Diamond may add per difficulty report.
    max_members_per_report: int = Field(default=3, gt=0)
    sandbox_backend: Literal["local", "docker"] = "docker"
    max_member_steps: int = Field(default=60, gt=0)
    max_member_actions_per_task: int = Field(default=20, gt=0)
    zap_enabled: bool = False
    browser_event_limit: int = Field(default=200, gt=0, le=1000)
    browser_console_limit: int = Field(default=100, gt=0, le=1000)
    browser_error_limit: int = Field(default=50, gt=0, le=1000)
    browser_response_preview_bytes: int = Field(default=4096, gt=0, le=16384)
    browser_allowed_origins: list[str] = Field(default_factory=list)
    browser_artifact_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)

    @field_validator("browser_allowed_origins")
    @classmethod
    def _validate_browser_origins(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip()
            parts = urlsplit(value)
            if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
                raise ValueError("browser allowed origins must use HTTP(S)")
            if parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ValueError("browser allowed origins must contain only scheme, host, and optional port")
            try:
                port = parts.port
            except ValueError as exc:
                raise ValueError("browser allowed origin contains an invalid port") from exc
            host = parts.hostname.lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            default_port = 80 if parts.scheme.lower() == "http" else 443
            suffix = f":{port}" if port is not None and port != default_port else ""
            origin = f"{parts.scheme.lower()}://{host}{suffix}"
            if origin not in normalized:
                normalized.append(origin)
        return normalized


class AppConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")

    log_enabled: bool = True
    diamond: LLMConfig = Field(default_factory=LLMConfig)
    members: list[MemberConfig] = Field(default_factory=list)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @field_validator("members")
    @classmethod
    def _name_members(cls, members: list[MemberConfig]) -> list[MemberConfig]:
        for idx, m in enumerate(members):
            if not m.name:
                m.name = MEMBER_NAMES[idx] if idx < len(MEMBER_NAMES) else f"member{idx}"
        names = [m.name for m in members]
        if len(set(names)) != len(names):
            raise ValueError("member names must be unique")
        return members

    @model_validator(mode="after")
    def _check_unique(self) -> AppConfig:
        # Keep the complete built-in worker roster visible in the Config UI.
        # Empty entries are harmless: ``available_members`` excludes them
        # until an API key/base URL (or mock format) is configured.
        by_name = {member.name: member for member in self.members}
        builtins = [by_name.pop(name, MemberConfig(name=name)) for name in MEMBER_NAMES]
        # Preserve any explicitly configured custom workers after the fixed
        # built-in roster.
        self.members = builtins + list(by_name.values())
        return self

    # --- startup validation (Seed.md launch rules) ---
    def startup_errors(self) -> list[str]:
        """Return human-readable reasons the system cannot start (empty if OK)."""
        errors: list[str] = []
        if not self.diamond.configured:
            errors.append("Diamond requires api_key and base_url (or api_format: mock).")
        if not self.members:
            errors.append("At least one Member must be configured.")
        elif not any(m.configured for m in self.members):
            errors.append("At least one Member must have api_key and base_url to start.")
        return errors

    def available_members(self) -> list[MemberConfig]:
        """Members that have credentials — the upper bound on parallelism."""
        return [m for m in self.members if m.configured]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _apply_models_defaults(cfg: AppConfig, models: dict[str, Any]) -> None:
    """Fill empty model fields from models.yaml defaults keyed by api_format."""
    defaults = models.get("defaults", {}) if isinstance(models, dict) else {}
    if cfg.diamond.model == "":
        cfg.diamond.model = defaults.get(cfg.diamond.api_format, "")
    for m in cfg.members:
        if m.model == "":
            m.model = defaults.get(m.api_format, "")


# Limit keys removed in the task-slot model. Silently dropped from old configs
# so an existing config.yaml/limits.yaml does not fail extra="forbid" validation.
_LEGACY_LIMIT_KEYS = ("total_memory_gb", "total_disk_gb", "per_agent_memory_gb")

# Runtime keys removed with the member difficulty-feedback mechanism.
_LEGACY_RUNTIME_KEYS = ("eval_interval_steps",)


def _strip_legacy_limits(limits: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in limits.items() if k not in _LEGACY_LIMIT_KEYS}


def _strip_legacy_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in runtime.items() if k not in _LEGACY_RUNTIME_KEYS}


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and merge config.yaml + models.yaml + limits.yaml."""
    base = config_dir or CONFIG_DIR
    raw = _load_yaml(base / "config.yaml")
    limits = _load_yaml(base / "limits.yaml")
    models = _load_yaml(base / "models.yaml")

    if limits:
        raw.setdefault("limits", limits.get("limits", limits))

    if isinstance(raw.get("limits"), dict):
        raw["limits"] = _strip_legacy_limits(raw["limits"])

    if isinstance(raw.get("runtime"), dict):
        raw["runtime"] = _strip_legacy_runtime(raw["runtime"])

    cfg = AppConfig.model_validate(raw)
    _apply_models_defaults(cfg, models)

    # Allow env override of the log switch (handy for docker / tests).
    env_log = os.environ.get("IPC_LOG_ENABLED")
    if env_log is not None:
        cfg.log_enabled = env_log.strip().lower() in ("1", "true", "yes", "on")
    env_zap = os.environ.get("IPC_ZAP_ENABLED")
    if env_zap is not None and env_zap.strip():
        cfg.runtime.zap_enabled = env_zap.strip().lower() in ("1", "true", "yes", "on")
    return cfg


def save_config(cfg: AppConfig, config_dir: Path | None = None) -> None:
    base = config_dir or CONFIG_DIR
    base.mkdir(parents=True, exist_ok=True)
    data = {
        "log_enabled": cfg.log_enabled,
        "diamond": cfg.diamond.model_dump(),
        "members": [m.model_dump() for m in cfg.members],
        "runtime": cfg.runtime.model_dump(),
        "limits": cfg.limits.model_dump(),
    }
    (base / "config.yaml").write_text(
        yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
