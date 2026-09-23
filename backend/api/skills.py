from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import get_state
from backend.skills.store import DEFAULT_REPOSITORY, DEFAULT_REVISION

router = APIRouter(prefix="/skills", tags=["skills"])


class InstallSkill(BaseModel):
    repository: str = Field(default=DEFAULT_REPOSITORY, max_length=200)
    revision: str = Field(default=DEFAULT_REVISION, pattern=r"^[a-f0-9]{40}$")


class EnableSkill(BaseModel):
    repository: str
    enabled: bool


@router.get("")
def list_skills(state=Depends(get_state)):
    return {"packages": state.skills.packages(), "default_repository": DEFAULT_REPOSITORY, "default_revision": DEFAULT_REVISION}


@router.post("")
def install_skill(body: InstallSkill, state=Depends(get_state)):
    try:
        return state.skills.install(body.repository, body.revision)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Skill download or validation failed") from exc


@router.patch("")
def enable_skill(body: EnableSkill, state=Depends(get_state)):
    try:
        state.skills.enable(body.repository, body.enabled)
    except KeyError as exc:
        raise HTTPException(404, "Skill package not installed") from exc
    return list_skills(state)
