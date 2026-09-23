from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.api.deps import get_state
from backend.core.config import (
    AppConfig,
    LLMConfig,
    ApiFormat,
    ApiSurface,
    CATEGORIES,
    SelectedReasoningEffort,
    RuntimeConfig,
)
from backend.core.state import AppState

router = APIRouter(tags=["config"])


class LLMUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_format: ApiFormat | None = None
    api_surface: ApiSurface | None = None
    reasoning_effort: SelectedReasoningEffort | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class RuntimeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    browser_event_limit: int | None = None
    browser_console_limit: int | None = None
    browser_error_limit: int | None = None
    browser_response_preview_bytes: int | None = None
    browser_allowed_origins: list[str] | None = None
    browser_artifact_max_bytes: int | None = None


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_enabled: bool | None = None
    diamond: LLMUpdate | None = None
    member: LLMUpdate | None = None
    use_legacy_member: str | None = None
    members: dict[str, LLMUpdate] | None = None  # keyed by member name
    remove_members: list[str] | None = None
    runtime: RuntimeUpdate | None = None


def _redact(value: str) -> str:
    if not value:
        return ""
    return value[:3] + "***" if len(value) > 4 else "***"


def _config_view(state: AppState) -> dict:
    cfg = state.config
    shared = cfg.member or LLMConfig()
    return {
        "member_config_conflict": cfg.member_config_conflict,
        "member": {
            **shared.model_dump(exclude={"api_key"}),
            "api_key_set": bool(shared.api_key),
            "api_key_preview": _redact(shared.api_key),
            "configured": shared.configured,
        },
        "log_enabled": cfg.log_enabled,
        "categories": list(CATEGORIES),
        "diamond": {
            "api_format": cfg.diamond.api_format,
            "api_surface": cfg.diamond.api_surface,
            "reasoning_effort": cfg.diamond.reasoning_effort,
            "api_key_set": bool(cfg.diamond.api_key),
            "api_key_preview": _redact(cfg.diamond.api_key),
            "base_url": cfg.diamond.base_url,
            "model": cfg.diamond.model,
            "configured": cfg.diamond.configured,
        },
        "members": [
            {
                "name": m.name,
                "api_format": m.api_format,
                "api_surface": m.api_surface,
                "reasoning_effort": m.reasoning_effort,
                "api_key_set": bool(m.api_key),
                "api_key_preview": _redact(m.api_key),
                "base_url": m.base_url,
                "model": m.model,
                "configured": m.configured,
            }
            for m in cfg.members
        ],
        "runtime": {
            "browser_event_limit": cfg.runtime.browser_event_limit,
            "browser_console_limit": cfg.runtime.browser_console_limit,
            "browser_error_limit": cfg.runtime.browser_error_limit,
            "browser_response_preview_bytes": cfg.runtime.browser_response_preview_bytes,
            "browser_allowed_origins": cfg.runtime.browser_allowed_origins,
            "browser_artifact_max_bytes": cfg.runtime.browser_artifact_max_bytes,
        },
        "startup_errors": cfg.startup_errors(),
    }


@router.get("/config")
def get_config(state: AppState = Depends(get_state)):
    return _config_view(state)


@router.get("/config/runtime")
def get_runtime_config(state: AppState = Depends(get_state)):
    orchestrator = state.orchestrator
    active_members: dict[str, list[str]] = {}
    running_tasks: list[dict[str, str]] = []
    if orchestrator is not None:
        with orchestrator._lock:
            active_members = {
                project_id: sorted(members.keys())
                for project_id, members in orchestrator._members.items()
                if members
            }
            running_tasks = [
                {"project_id": project_id, "intent_id": intent_id}
                for (project_id, intent_id), future in orchestrator._task_index.items()
                if not future.done()
            ]
    return {
        "runtime": state.config.runtime.model_dump(),
        "limits": state.config.limits.model_dump(),
        "limiter": {
            "max_concurrent_tasks": state.limiter.max_concurrent_tasks,
            "active_tasks": state.limiter.active_tasks(),
        },
        "pool": {
            "active_keys": state.pool.active_keys(),
        },
        "orchestrator": {
            "active_members": active_members,
            "running_tasks": running_tasks,
        },
    }


def _apply(llm, upd: LLMUpdate) -> None:
    if upd.api_format is not None:
        llm.api_format = upd.api_format
    if upd.api_surface is not None:
        llm.api_surface = upd.api_surface
    if upd.reasoning_effort is not None:
        llm.reasoning_effort = upd.reasoning_effort
    if upd.api_key is not None:
        llm.api_key = upd.api_key
    if upd.base_url is not None:
        llm.base_url = upd.base_url
    if upd.model is not None:
        llm.model = upd.model


@router.put("/config")
def update_config(body: ConfigUpdate, state: AppState = Depends(get_state)):
    cfg = state.config.model_copy(deep=True)
    if body.remove_members:
        raise HTTPException(400, "The ten Member identities are fixed.")
    if body.members:
        raise HTTPException(400, "Use the shared member configuration instead of per-member endpoints.")
    if body.use_legacy_member is not None:
        if body.member is not None or not cfg.member_config_conflict:
            raise HTTPException(400, "Select a legacy endpoint only while resolving a migration conflict.")
        selected = next((m for m in cfg.members if m.name == body.use_legacy_member), None)
        if selected is None:
            raise HTTPException(400, "Legacy Member endpoint not found.")
        cfg.member = LLMConfig.model_validate(selected.model_dump(exclude={"name"}))
        cfg.member_config_conflict = False
    if body.member is not None:
        shared = cfg.member.model_copy(deep=True) if cfg.member else LLMConfig()
        _apply(shared, body.member)
        cfg.member = shared
        cfg.member_config_conflict = False
    if body.log_enabled is not None:
        cfg.log_enabled = body.log_enabled
    if body.diamond is not None:
        _apply(cfg.diamond, body.diamond)
    if body.runtime is not None:
        runtime_values = {
            **cfg.runtime.model_dump(),
            **body.runtime.model_dump(exclude_none=True),
        }
        try:
            cfg.runtime = RuntimeConfig.model_validate(runtime_values)
        except ValidationError as exc:
            # Pydantic's default string includes rejected input, which can
            # contain credentials accidentally pasted into an origin URL.
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_context=False, include_url=False)
            )
            raise HTTPException(400, detail) from exc
    state.config = AppConfig.model_validate(cfg.model_dump())
    state.save_config()
    return _config_view(state)


@router.post("/config/health")
def health_check(state: AppState = Depends(get_state)):
    """Validate each configured LLM endpoint (Seed.md 启动时需要校验每个 LLM 的 health)."""
    from backend.members.adapters import health_check as adapter_health

    results = {}
    if state.config.diamond.configured:
        results["diamond"] = adapter_health(state.config.diamond)
    else:
        results["diamond"] = {"ok": False, "skipped": True, "reason": "API key/base URL not configured"}
    for m in [state.config.member] if state.config.member else []:
        if m.configured:
            results["member"] = adapter_health(m)
        else:
            results["member"] = {"ok": False, "skipped": True, "reason": "API key/base URL not configured"}
    return {"results": results, "startup_errors": state.config.startup_errors()}
