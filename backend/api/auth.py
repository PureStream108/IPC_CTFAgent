from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.auth.manager import (
    SESSION_COOKIE_NAME,
    AlreadyConfiguredError,
    AuthConfigurationError,
    AuthManager,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=1024)

    @field_validator("password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("password cannot contain only whitespace")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(default="admin", max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def get_auth_manager(request: Request) -> AuthManager:
    return request.app.state.auth


def _cookie_is_secure(request: Request) -> bool:
    override = request.app.state.auth_secure_cookie
    return override if override is not None else request.url.scheme == "https"


def _set_session_cookie(response: Response, request: Request, manager: AuthManager) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=manager.create_session(),
        max_age=manager.session_ttl_seconds,
        path="/",
        secure=_cookie_is_secure(request),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/status")
def auth_status(request: Request, manager: AuthManager = Depends(get_auth_manager)) -> dict:
    # Authentication is intentionally disabled for the trusted Docker UI.
    return {
        "setup_required": False,
        "authenticated": True,
        "username": None,
    }


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup(
    payload: SetupRequest,
    request: Request,
    response: Response,
    manager: AuthManager = Depends(get_auth_manager),
) -> dict:
    if manager.configuration_error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication configuration is unavailable",
        )
    try:
        manager.setup(payload.password)
    except AlreadyConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _set_session_cookie(response, request, manager)
    return {"setup_required": False, "authenticated": True, "username": "admin"}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    manager: AuthManager = Depends(get_auth_manager),
) -> dict:
    if manager.configuration_error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication configuration is unavailable",
        )
    if manager.setup_required:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="initial setup is required",
        )
    client_key = _client_key(request)
    retry_after = manager.login_retry_after(client_key)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    try:
        valid_password = manager.verify_password(payload.password)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication configuration is unavailable",
        ) from exc
    if not valid_password or payload.username != "admin":
        manager.record_login_failure(client_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid username or password",
            headers={"WWW-Authenticate": "Cookie"},
        )
    manager.clear_login_failures(client_key)
    _set_session_cookie(response, request, manager)
    return {"setup_required": False, "authenticated": True, "username": "admin"}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    manager: AuthManager = Depends(get_auth_manager),
) -> None:
    manager.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=_cookie_is_secure(request),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
