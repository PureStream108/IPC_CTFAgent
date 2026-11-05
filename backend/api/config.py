from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.api.deps import get_state
from backend.core.config import (
    CATEGORIES,
    ApiFormat,
    ApiSurface,
    AppConfig,
    LLMConfig,
    RuntimeConfig,
    SelectedReasoningEffort,
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


class LimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_tasks: int | None = None
    total_cpu: int | None = None


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_enabled: bool | None = None
    diamond: LLMUpdate | None = None
    member: LLMUpdate | None = None
    member_backup: LLMUpdate | None = None
    clear_member_backup: bool = False
    use_legacy_member: str | None = None
    members: dict[str, LLMUpdate] | None = None
    remove_members: list[str] | None = None
    runtime: RuntimeUpdate | None = None
    limits: LimitsUpdate | None = None


def _redact(value: str) -> str:
    if not value:
        return ""
    return value[:3] + "***" if len(value) > 4 else "***"


def _member_key_slots(cfg: AppConfig) -> dict[str, str]:
    """Which key each fixed Member seat uses (odd index -> backup)."""
    return cfg.member_key_slots()


def _config_view(state: AppState) -> dict:
    cfg = state.config
    shared = cfg.member or LLMConfig()
    backup = cfg.member_backup
    return {
        "member_config_conflict": cfg.member_config_conflict,
        "member": {
            **shared.model_dump(exclude={"api_key"}),
            "api_key_set": bool(shared.api_key),
            "api_key_preview": _redact(shared.api_key),
            "configured": shared.configured,
        },
        "member_backup": (
            {
                **backup.model_dump(exclude={"api_key"}),
                "api_key_set": bool(backup.api_key),
                "api_key_preview": _redact(backup.api_key),
                "configured": backup.configured,
            }
            if backup is not None
            else None
        ),
        "member_key_slots": _member_key_slots(cfg),
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
        "runtime": {},
        "limits": cfg.limits.model_dump(),
        "startup_errors": cfg.startup_errors(),
        "startup_warnings": cfg.startup_warnings(),
    }



_GET_STATE_DEPENDENCY = Depends(get_state)

@router.get("/config")
def get_config(state: AppState = _GET_STATE_DEPENDENCY):
    return _config_view(state)


@router.get("/config/runtime")
def get_runtime_config(state: AppState = _GET_STATE_DEPENDENCY):
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
def update_config(body: ConfigUpdate, state: AppState = _GET_STATE_DEPENDENCY):
    cfg = state.config.model_copy(deep=True)
    if body.remove_members:
        raise HTTPException(400, "The ten Member identities are fixed.")
    if body.members:
        raise HTTPException(400, "Use the shared member configuration instead of per-member endpoints.")
    if body.clear_member_backup and body.member_backup is not None:
        raise HTTPException(400, "Set or clear the Member backup slot, not both.")
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
    if body.clear_member_backup:
        cfg.member_backup = None
    elif body.member_backup is not None:
        backup = cfg.member_backup.model_copy(deep=True) if cfg.member_backup else LLMConfig()
        _apply(backup, body.member_backup)
        cfg.member_backup = backup
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
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_context=False, include_url=False)
            )
            raise HTTPException(400, detail) from exc
    if body.limits is not None:
        limit_values = {
            **cfg.limits.model_dump(),
            **body.limits.model_dump(exclude_none=True),
        }
        try:
            cfg.limits = type(cfg.limits).model_validate(limit_values)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_context=False, include_url=False)
            )
            raise HTTPException(400, detail) from exc
    state.config = AppConfig.model_validate(cfg.model_dump())
    state.save_config()
    return _config_view(state)


@router.post("/config/health")
def health_check(state: AppState = _GET_STATE_DEPENDENCY):
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
    backup = state.config.member_backup
    if backup is not None and backup.configured:
        results["member_backup"] = adapter_health(backup)
    else:
        results["member_backup"] = {
            "ok": False,
            "skipped": True,
            "reason": "API key/base URL not configured" if backup is not None else "Backup slot not configured",
        }
    return {"results": results, "startup_errors": state.config.startup_errors()}
