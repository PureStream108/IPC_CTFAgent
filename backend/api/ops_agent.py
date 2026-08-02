from __future__ import annotations

import json
import threading
from typing import Any, Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.api.deps import get_state
from backend.core.config import ApiFormat, ApiSurface, ReasoningEffort
from backend.core.state import AppState
from backend.ops.network import NetworkPolicyError
from backend.ops.service import (
    OpsAgentNotConfigured,
    OpsAgentService,
    OpsAgentUpstreamError,
    WorkflowConfirmationError,
)

router = APIRouter(prefix="/api/ops", tags=["ipc-agent"])
_SERVICE_LOCK = threading.Lock()


class OpsConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_format: ApiFormat | None = None
    api_surface: ApiSurface | None = None
    reasoning_effort: ReasoningEffort | None = None
    api_key: str | None = Field(default=None, max_length=16_384)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=256)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, pattern=r"^ops_[a-f0-9]{16}$")
    secrets: dict[str, str] = Field(default_factory=dict)


class ChatInterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=r"^ops_[a-f0-9]{16}$")
    run_id: str = Field(pattern=r"^run_[A-Za-z0-9_-]{8,128}$")


class WorkflowDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: dict[str, Any]
    secrets: dict[str, str] = Field(default_factory=dict)


class WorkflowSecretsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secrets: dict[str, str]


class WorkflowConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_phrase: str = Field(min_length=1, max_length=256)


class WorkflowExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_token: str = Field(min_length=20, max_length=256)
    operation: Literal["preview", "import", "submit"]
    select: list[str] | None = None
    project_id: str | None = None
    external_id: str | None = None
    flag: str | None = None

    @field_validator("select")
    @classmethod
    def validate_select(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) > 1000:
            raise ValueError("select is limited to 1000 challenge ids")
        return value


def get_ops_service(state: AppState = Depends(get_state)) -> OpsAgentService:
    service = getattr(state, "ops_agent_service", None)
    if service is not None:
        return service
    with _SERVICE_LOCK:
        service = getattr(state, "ops_agent_service", None)
        if service is None:
            service = OpsAgentService(state)
            state.ops_agent_service = service
    return service


@router.get("/config")
def get_config(service: OpsAgentService = Depends(get_ops_service)):
    return service.config_view()


@router.put("/config")
def update_config(body: OpsConfigUpdate, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.update_config, **body.model_dump(exclude_unset=True))


@router.post("/config/health")
def health(service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.health)


@router.get("/tools")
def tools(service: OpsAgentService = Depends(get_ops_service)):
    return {"tools": service.tool_catalog()}


@router.get("/sessions")
def list_sessions(service: OpsAgentService = Depends(get_ops_service)):
    return {"sessions": service.list_sessions()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.session_view, session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, service: OpsAgentService = Depends(get_ops_service)):
    try:
        deleted = service.delete_session(session_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "IPC session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chat")
def chat(body: ChatRequest, service: OpsAgentService = Depends(get_ops_service)):
    return _call(
        service.chat,
        message=body.message,
        session_id=body.session_id,
        secrets_values=body.secrets,
    )


@router.post("/chat/stream")
def chat_stream(body: ChatRequest, service: OpsAgentService = Depends(get_ops_service)):
    def events():
        for event in service.chat_stream(
            message=body.message,
            session_id=body.session_id,
            secrets_values=body.secrets,
        ):
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/interrupt")
def interrupt_chat(body: ChatInterruptRequest, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.interrupt_chat, session_id=body.session_id, run_id=body.run_id)


@router.get("/workflows")
def list_workflows(service: OpsAgentService = Depends(get_ops_service)):
    return {"workflows": service.list_workflows()}


@router.post("/workflows", status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowDraftRequest, service: OpsAgentService = Depends(get_ops_service)):
    return _call(
        service.create_workflow,
        body.workflow,
        secrets_values=body.secrets,
    )


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.get_workflow, workflow_id)


@router.put("/workflows/{workflow_id}")
def update_workflow(
    workflow_id: str,
    body: WorkflowDraftRequest,
    service: OpsAgentService = Depends(get_ops_service),
):
    return _call(
        service.update_workflow,
        workflow_id,
        body.workflow,
        secrets_values=body.secrets,
    )


@router.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: str, service: OpsAgentService = Depends(get_ops_service)):
    if not service.delete_workflow(workflow_id):
        raise HTTPException(404, "workflow not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/workflows/{workflow_id}/secrets")
def update_workflow_secrets(
    workflow_id: str,
    body: WorkflowSecretsUpdate,
    service: OpsAgentService = Depends(get_ops_service),
):
    return _call(service.set_workflow_secrets, workflow_id, body.secrets)


@router.get("/workflows/{workflow_id}/confirmation")
def confirmation_preview(workflow_id: str, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.confirmation_preview, workflow_id)


@router.post("/workflows/{workflow_id}/confirm")
def confirm_workflow(
    workflow_id: str,
    body: WorkflowConfirmRequest,
    service: OpsAgentService = Depends(get_ops_service),
):
    return _call(service.confirm_workflow, workflow_id, body.confirmation_phrase)


@router.post("/workflows/{workflow_id}/revoke")
def revoke_workflow(workflow_id: str, service: OpsAgentService = Depends(get_ops_service)):
    return _call(service.revoke_workflow, workflow_id)


@router.post("/workflows/{workflow_id}/execute")
def execute_workflow(
    workflow_id: str,
    body: WorkflowExecuteRequest,
    service: OpsAgentService = Depends(get_ops_service),
):
    return _call(
        service.execute,
        workflow_id,
        execution_token=body.execution_token,
        operation=body.operation,
        select=body.select,
        project_id=body.project_id,
        external_id=body.external_id,
        flag=body.flag,
    )


def _call(function, *args: Any, **kwargs: Any):
    try:
        return function(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "IPC resource not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except OpsAgentNotConfigured as exc:
        raise HTTPException(409, str(exc)) from exc
    except OpsAgentUpstreamError as exc:
        raise HTTPException(502, str(exc)) from exc
    except WorkflowConfirmationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except NetworkPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except requests.RequestException as exc:
        raise HTTPException(502, f"workflow network request failed: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
