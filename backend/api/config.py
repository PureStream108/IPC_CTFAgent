from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from backend.api.deps import get_state
from backend.core.config import ApiFormat, ApiSurface, CATEGORIES, MemberConfig, ReasoningEffort
from backend.core.state import AppState

router = APIRouter(tags=["config"])


class LLMUpdate(BaseModel):
    api_format: ApiFormat | None = None
    api_surface: ApiSurface | None = None
    reasoning_effort: ReasoningEffort | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class RuntimeUpdate(BaseModel):
    zap_enabled: bool | None = None


class ConfigUpdate(BaseModel):
    log_enabled: bool | None = None
    diamond: LLMUpdate | None = None
    members: dict[str, LLMUpdate] | None = None  # keyed by member name
    remove_members: list[str] | None = None
    runtime: RuntimeUpdate | None = None


def _redact(value: str) -> str:
    if not value:
        return ""
    return value[:3] + "***" if len(value) > 4 else "***"


def _config_view(state: AppState) -> dict:
    cfg = state.config
    return {
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
            "zap_enabled": cfg.runtime.zap_enabled,
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
    cfg = state.config
    remove_names: set[str] = set()
    for raw_name in body.remove_members or []:
        try:
            name = MemberConfig(name=raw_name).name
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not name:
            raise HTTPException(400, "member name cannot be empty")
        remove_names.add(name)
    normalized_updates: dict[str, LLMUpdate] = {}
    for raw_name, upd in (body.members or {}).items():
        try:
            name = MemberConfig(name=raw_name).name
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not name:
            raise HTTPException(400, "member name cannot be empty")
        if name in normalized_updates:
            raise HTTPException(400, f"duplicate member name: {name}")
        normalized_updates[name] = upd

    if body.log_enabled is not None:
        cfg.log_enabled = body.log_enabled
    if body.diamond is not None:
        _apply(cfg.diamond, body.diamond)
    if body.runtime is not None and body.runtime.zap_enabled is not None:
        cfg.runtime.zap_enabled = body.runtime.zap_enabled
    if remove_names:
        cfg.members = [member for member in cfg.members if member.name not in remove_names]
    if normalized_updates:
        by_name = {m.name: m for m in cfg.members}
        for name, upd in normalized_updates.items():
            member = by_name.get(name)
            if member is None:
                member = MemberConfig(name=name)
                cfg.members.append(member)
                by_name[name] = member
            _apply(member, upd)
    state.save_config()
    return _config_view(state)


@router.post("/config/health")
def health_check(state: AppState = Depends(get_state)):
    """Validate each configured LLM endpoint (Seed.md 启动时需要校验每个 LLM 的 health)."""
    from backend.members.adapters import health_check as adapter_health

    results = {}
    results["diamond"] = adapter_health(state.config.diamond)
    for m in state.config.members:
        results[m.name] = adapter_health(m)
    return {"results": results, "startup_errors": state.config.startup_errors()}
