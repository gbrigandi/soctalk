"""Authentication endpoints (opt-in)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from soctalk.api.auth import (
    UserIdentity,
    clear_session_cookie,
    get_auth_mode,
    get_optional_user,
    parse_static_users,
    require_authenticated,
    set_session_cookie,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class UserInfo(BaseModel):
    username: str
    roles: list[str]
    source: str


class SessionStatus(BaseModel):
    enabled: bool
    mode: str
    user: UserInfo | None


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=4096)


class LoginResponse(BaseModel):
    user: UserInfo


@router.get("/session", response_model=SessionStatus)
async def get_session(request: Request) -> SessionStatus:
    mode = get_auth_mode()
    user = get_optional_user(request)
    return SessionStatus(
        enabled=mode != "none",
        mode=mode,
        user=(
            UserInfo(username=user.username, roles=sorted(user.roles), source=user.source)
            if user
            else None
        ),
    )


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
    mode = get_auth_mode()
    if mode == "none":
        raise HTTPException(status_code=400, detail="Authentication is disabled")
    if mode != "static":
        raise HTTPException(status_code=400, detail="Login is not available for this auth mode")

    users = parse_static_users()
    if not users:
        raise HTTPException(status_code=500, detail="AUTH_USERS is not configured")

    record = users.get(payload.username)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        ok = verify_password(payload.password, record.password_hash)
    except ValueError:
        raise HTTPException(status_code=500, detail="Invalid password hash configuration")

    if not ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user = UserIdentity(username=record.username, roles=record.roles, source="static")
    set_session_cookie(response, user)
    return LoginResponse(user=UserInfo(username=user.username, roles=sorted(user.roles), source=user.source))


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    mode = get_auth_mode()
    if mode == "static":
        clear_session_cookie(response)
        return {"success": True}

    # For proxy mode, logout is handled upstream.
    if mode == "proxy":
        require_authenticated(request)
        return {"success": True}

    return {"success": True}

