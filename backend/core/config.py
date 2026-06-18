from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# CTF challenge categories the user may pick when creating a project.
Category = Literal["pwn", "reverse", "crypto", "web", "misc", "ai", "osint"]
CATEGORIES: tuple[str, ...] = ("pwn", "reverse", "crypto", "web", "misc", "ai", "osint")

# Supported LLM wire formats. base_url is always user-provided.
ApiFormat = Literal["openai", "claudecode", "deepseek", "pi", "mock"]

# Default member names. These are worker identities, not different roles.
MEMBER_NAMES: tuple[str, ...] = ("aventurine", "pearl", "jade", "topaz")

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class LLMConfig(BaseModel):
    """Per-agent LLM endpoint config (Diamond or a Member)."""

    model_config = ConfigDict(extra="forbid")

    api_format: ApiFormat = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    @property
    def configured(self) -> bool:
        """A mock agent needs no creds; everyone else needs key + base_url."""
        if self.api_format == "mock":
            return True
        return bool(self.api_key.strip()) and bool(self.base_url.strip())


class MemberConfig(LLMConfig):
    name: str = ""


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_cpu: int = 4
    total_memory_gb: int = 20
    total_disk_gb: int = 25
    per_agent_memory_gb: int = 5
    network: bool = True


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Difficulty self-evaluation cadence for Members.
    eval_interval_steps: int = Field(default=20, gt=0)
    # Scheduler tick + heartbeat cadence (seconds).
    interval: int = Field(default=2, gt=0)
    intent_timeout: int = Field(default=30, ge=5)
    reason_timeout: int = Field(default=30, ge=5)
    # Max extra Members Diamond may add per difficulty report.
    max_members_per_report: int = Field(default=3, gt=0)
    sandbox_backend: Literal["local", "docker"] = "docker"
    max_member_steps: int = Field(default=60, gt=0)
    max_member_actions_per_task: int = Field(default=20, gt=0)


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
            m.name = m.name.strip().lower()
        names = [m.name for m in members]
        if len(set(names)) != len(names):
            raise ValueError("member names must be unique")
        return members

    @model_validator(mode="after")
    def _check_unique(self) -> "AppConfig":
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


def load_config(config_dir: Path | None = None) -> AppConfig:
    """Load and merge config.yaml + models.yaml + limits.yaml."""
    base = config_dir or CONFIG_DIR
    raw = _load_yaml(base / "config.yaml")
    limits = _load_yaml(base / "limits.yaml")
    models = _load_yaml(base / "models.yaml")

    if limits:
        raw.setdefault("limits", limits.get("limits", limits))

    cfg = AppConfig.model_validate(raw)
    _apply_models_defaults(cfg, models)

    # Allow env override of the log switch (handy for docker / tests).
    env_log = os.environ.get("IPC_LOG_ENABLED")
    if env_log is not None:
        cfg.log_enabled = env_log.strip().lower() in ("1", "true", "yes", "on")
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
