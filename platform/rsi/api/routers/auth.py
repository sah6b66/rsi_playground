"""Auth endpoints (DD-13 §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from rsi.api.deps import get_current_user
from rsi.core.errors import AuthError
from rsi.security.auth import Claims

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_at: int
    user_id: str
    role: str


class MeResponse(BaseModel):
    user_id: str
    role: str
    expires_at: int


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginBody, request: Request) -> LoginResponse:
    services = request.app.state.services
    users: dict = services.get("users", {})
    record = users.get(body.username)
    if record is None:
        raise AuthError("invalid credentials")
    auth = services["auth_service"]
    token, claims = auth.login(
        user_id=record["user_id"],
        role=record["role"],
        stored_hash=record["password_hash"],
        password=body.password,
    )
    return LoginResponse(
        token=token,
        expires_at=claims.exp,
        user_id=claims.sub,
        role=claims.role,
    )


@router.get("/me", response_model=MeResponse)
async def me(user: Claims = Depends(get_current_user)) -> MeResponse:
    return MeResponse(user_id=user.sub, role=user.role, expires_at=user.exp)
