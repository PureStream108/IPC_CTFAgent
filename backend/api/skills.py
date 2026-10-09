from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.api.deps import get_state
from backend.skills.store import (
    DEFAULT_REPOSITORY,
    DEFAULT_REVISION,
    FOLDERS,
    MAX_UPLOAD_BYTES,
)

router = APIRouter(prefix="/skills", tags=["skills"])


class InstallSkill(BaseModel):
    repository: str = Field(default=DEFAULT_REPOSITORY, max_length=200)
    revision: str = Field(default=DEFAULT_REVISION, pattern=r"^[a-f0-9]{40}$")


class EnableSkill(BaseModel):
    repository: str
    enabled: bool


class AddSkill(BaseModel):
    """``npx skills add <spec>`` where spec is owner/repo or a .git URL."""

    spec: str = Field(min_length=1, max_length=200)


class MoveSkill(BaseModel):
    repository: str = Field(max_length=200)
    path: str = Field(max_length=500)
    folder: str = Field(max_length=32)


def _view(state):
    return {
        "packages": state.skills.packages(),
        "tree": state.skills.tree(),
        "folders": list(FOLDERS),
        "default_repository": DEFAULT_REPOSITORY,
        "default_revision": DEFAULT_REVISION,
    }


@router.get("")
def list_skills(state=Depends(get_state)):
    return _view(state)


@router.post("")
def install_skill(body: InstallSkill, state=Depends(get_state)):
    try:
        state.skills.install(body.repository, body.revision)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Skill download or validation failed") from exc
    return _view(state)


@router.post("/add")
def add_skill(body: AddSkill, state=Depends(get_state)):
    """Install a skill package with the ``skills`` CLI."""
    try:
        result = state.skills.install_with_npx(body.spec)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {**_view(state), "output": result["output"], "spec": result["spec"]}


@router.post("/upload")
async def upload_skill(file: UploadFile = File(...), state=Depends(get_state)):
    """Accept an operator-provided SKILL.md into the Custom folder."""
    if not file.filename:
        raise HTTPException(400, "a SKILL.md filename is required")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"SKILL.md exceeds {MAX_UPLOAD_BYTES} bytes")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "SKILL.md must be UTF-8 text") from exc
    name = file.filename
    if name.lower() in ("skill.md", "skill"):
        raise HTTPException(400, "rename the file so the skill has a distinct name")
    try:
        state.skills.upload(name, content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _view(state)


@router.post("/move")
def move_skill(body: MoveSkill, state=Depends(get_state)):
    """Reassign a skill to another folder (drag-and-drop in the UI)."""
    try:
        state.skills.move(body.repository, body.path, body.folder)
    except KeyError as exc:
        raise HTTPException(404, "Skill not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _view(state)


@router.patch("")
def enable_skill(body: EnableSkill, state=Depends(get_state)):
    try:
        state.skills.enable(body.repository, body.enabled)
    except KeyError as exc:
        raise HTTPException(404, "Skill package not installed") from exc
    return _view(state)
