from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# CTF challenge categories the user may pick when creating a project.
CATEGORIES: tuple[str, ...] = ("pwn", "reverse", "crypto", "web", "misc", "ai", "osint")

# Supported LLM wire formats. base_url is always user-provided.
# ``anthropic`` is the raw Messages API. There is no separate sidecar runtime:
# every agent runs through the unified loop in backend/agent/runtime.py.
ApiFormat = Literal["openai", "anthropic", "deepseek", "pi", "mock"]
ApiSurface = Literal["auto", "chat_completions", "responses"]
ReasoningEffort = Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
SelectedReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

# Default member names. These are worker identities, not different roles.
MEMBER_NAMES: tuple[str, ...] = (
    "amber", "agate", "topaz", "sugilite", "aventurine",
    "pearl", "sapphire", "jade", "obsidian", "opal",
)
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
    reasoning_effort: ReasoningEffort = "high"

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
    max_concurrent_tasks: int = Field(default=10, gt=0, le=10)
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
    sandbox_backend_forced: bool = False
    max_member_steps: int = Field(default=60, gt=0)
    max_member_actions_per_task: int = Field(default=20, gt=0)
    # Agent runtime budgets. A long investigation is normal work, so none of
    # these impose a hard step ceiling: convergence comes from the provider
    # finishing, repeated-batch detection, cancellation and the watchdog.
    # ``max_turns = 0`` means no ceiling.
    max_output_tokens: int = Field(default=32768, gt=0)
    # 0 infers the window from the model instead of guessing a fixed number.
    context_token_limit: int = Field(default=0, ge=0)
    compaction_trigger_ratio: float = Field(default=0.75, gt=0.0, le=0.95)
    max_turns: int = Field(default=0, ge=0)
    # Chunk-gap timeout for streamed turns. Long reasoning can precede the
    # first token, so this is deliberately generous.
    provider_read_timeout: int = Field(default=300, ge=30)
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
    member: LLMConfig | None = None
    member_config_conflict: bool = False
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
        if self.member is None:
            # Compare complete legacy endpoints, never silently pick a key.
            blank = LLMConfig().model_dump()
            candidates = [m.model_dump(exclude={"name"}) for m in self.members
                          if m.model_dump(exclude={"name"}) != blank]
            if candidates and any(item != candidates[0] for item in candidates[1:]):
                self.member_config_conflict = True
            elif not self.member_config_conflict:
                self.member = LLMConfig.model_validate(candidates[0] if candidates else {})
        if self.member is not None:
            self.member_config_conflict = False
            self.members = [MemberConfig(name=name, **self.member.model_dump()) for name in MEMBER_NAMES]
        return self

    # --- startup validation (Seed.md launch rules) ---
    def startup_errors(self) -> list[str]:
        """Return human-readable reasons the system cannot start (empty if OK)."""
        errors: list[str] = []
        if self.member_config_conflict:
            errors.append("Member configurations differ. Select one shared Member configuration in Config.")
        for label, llm in (("Diamond", self.diamond), ("Member", self.member)):
            if llm and llm.configured and llm.api_format != "mock" and llm.reasoning_effort in {"auto", "none", "minimal"}:
                errors.append(f"{label}: select low, medium, high, xhigh or max for the migrated reasoning setting.")
        if not self.diamond.configured:
            errors.append("Diamond requires api_key and base_url (or api_format: mock).")
        if not self.members:
            errors.append("At least one Member must be configured.")
        elif not any(m.configured for m in self.members):
            errors.append("At least one Member must have api_key and base_url to start.")
        return errors

    def startup_warnings(self) -> list[str]:
        """Non-blocking notices about how the runtime adjusted the configuration."""
        warnings: list[str] = []
        if self.runtime.sandbox_backend_forced:
            warnings.append(
                "Sandbox backend 'local' runs solver commands on the host filesystem and was "
                "upgraded to 'docker'. Set IPC_ALLOW_LOCAL_SANDBOX=1 only for offline development."
            )
        return warnings

    def available_members(self) -> list[MemberConfig]:
        """Members that have credentials — the upper bound on parallelism."""
        return [] if self.member_config_conflict else [m for m in self.members if m.configured]


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
    if cfg.member is not None and cfg.member.model == "":
        cfg.member.model = defaults.get(cfg.member.api_format, "")


# Limit keys removed in the task-slot model. Silently dropped from old configs
# so an existing config.yaml/limits.yaml does not fail extra="forbid" validation.
_LEGACY_LIMIT_KEYS = ("total_memory_gb", "total_disk_gb", "per_agent_memory_gb")

LOCAL_SANDBOX_ENV = "IPC_ALLOW_LOCAL_SANDBOX"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _enforce_container_sandbox(cfg: AppConfig) -> None:
    """Refuse the host-backed sandbox unless it was explicitly opted into.

    ``local`` executes member commands as subprocesses in the application's own
    filesystem namespace. A solver can then read host paths outside the task
    workspace (observed: reading an exported writeup PDF from the host artifact
    tree) and invoke host interpreters instead of the ones installed in the task
    image. Containment is the default; the opt-in stays for offline unit tests.
    """
    if cfg.runtime.sandbox_backend != "local":
        cfg.runtime.sandbox_backend_forced = False
        return
    if _env_flag(LOCAL_SANDBOX_ENV):
        cfg.runtime.sandbox_backend_forced = False
        return
    cfg.runtime.sandbox_backend = "docker"
    cfg.runtime.sandbox_backend_forced = True


def _strip_legacy_limits(limits: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in limits.items() if k not in _LEGACY_LIMIT_KEYS}


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
        raw["runtime"].pop("zap_enabled", None)

    cfg = AppConfig.model_validate(raw)
    _apply_models_defaults(cfg, models)
    _enforce_container_sandbox(cfg)

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
        "member": cfg.member.model_dump() if cfg.member is not None else None,
        "member_config_conflict": cfg.member_config_conflict,
        "members": [m.model_dump() for m in cfg.members] if cfg.member_config_conflict else [],
        "runtime": cfg.runtime.model_dump(exclude={"sandbox_backend_forced"}),
        "limits": cfg.limits.model_dump(),
    }
    # Serialize before touching disk, then publish a complete file on the same
    # filesystem. A failed write must preserve the previous configuration.
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    temporary: Path | None = None
    try:
        # NamedTemporaryFile uses owner-only permissions on POSIX: credentials
        # must not become world-readable while staging or after replacement.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=base,
            prefix=".config-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, base / "config.yaml")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
