from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_state
from backend.core.config import CATEGORIES, MEMBER_NAMES
from backend.core.state import AppState

router = APIRouter(tags=["config"])


class LLMUpdate(BaseModel):
    api_format: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


class ConfigUpdate(BaseModel):
    log_enabled: bool | None = None
    diamond: LLMUpdate | None = None
    members: dict[str, LLMUpdate] | None = None  # keyed by member name


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
                "api_key_set": bool(m.api_key),
                "api_key_preview": _redact(m.api_key),
                "base_url": m.base_url,
                "model": m.model,
                "configured": m.configured,
            }
            for m in cfg.members
        ],
        "startup_errors": cfg.startup_errors(),
    }


@router.get("/config")
def get_config(state: AppState = Depends(get_state)):
    return _config_view(state)


def _apply(llm, upd: LLMUpdate) -> None:
    if upd.api_format is not None:
        llm.api_format = upd.api_format
    if upd.api_key is not None:
        llm.api_key = upd.api_key
    if upd.base_url is not None:
        llm.base_url = upd.base_url
    if upd.model is not None:
        llm.model = upd.model


@router.put("/config")
def update_config(body: ConfigUpdate, state: AppState = Depends(get_state)):
    cfg = state.config
    if body.log_enabled is not None:
        cfg.log_enabled = body.log_enabled
    if body.diamond is not None:
        _apply(cfg.diamond, body.diamond)
    if body.members:
        by_name = {m.name: m for m in cfg.members}
        for name, upd in body.members.items():
            if name in by_name:
                _apply(by_name[name], upd)
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
