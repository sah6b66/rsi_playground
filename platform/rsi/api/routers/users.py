"""User management endpoints (interfaces §9.19). Admin only.

用户记录保存在 DI 容器的 ``users`` 服务（username -> {user_id, role,
password_hash, disabled}），与 AuthService/login 共用同一份数据；
密码仅存 argon2 哈希，永不回显。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, require_perms
from rsi.core.errors import ConflictError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/users", tags=["users"])

Role = Literal["admin", "agent_engineer", "researcher", "approver", "service"]


class UserCreate(BaseModel):
    username: str = Field(json_schema_extra={"example": "eng-1"})
    password: str = Field(json_schema_extra={"example": "s3cret!"})
    role: Role = Field(json_schema_extra={"example": "agent_engineer"})


class UserUpdate(BaseModel):
    role: Role | None = None
    disabled: bool | None = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=8)


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    disabled: bool = False


class UserPage(BaseModel):
    items: list[UserOut]


@router.get("", response_model=UserPage)
async def list_users(
    request: Request,
    user: Claims = Depends(require_perms("user:manage")),
) -> UserPage:
    users: dict = request.app.state.services.get("users", {})
    items = [
        UserOut(
            id=rec.get("user_id", username),
            username=username,
            role=rec.get("role", ""),
            disabled=bool(rec.get("disabled", False)),
        )
        for username, rec in users.items()
        if isinstance(rec, dict) and "user_id" in rec
    ]
    return UserPage(items=items)


@router.post("", response_model=UserOut, status_code=201)
@audit.audited("user.create", object_type="user")
async def create_user(
    body: UserCreate,
    request: Request,
    user: Claims = Depends(require_perms("user:manage")),
) -> UserOut:
    services = request.app.state.services
    users: dict = services.get("users", {})
    if body.username in users:
        raise ConflictError(
            f"username {body.username!r} already exists",
            context={"username": body.username},
        )
    auth = services["auth_service"]
    rec = {
        "user_id": f"u-{body.username}",
        "role": body.role,
        "password_hash": auth.hash_password(body.password),
        "disabled": False,
    }
    users[body.username] = rec
    return UserOut(
        id=rec["user_id"], username=body.username, role=body.role, disabled=False
    )


@router.put("/{user_id}", status_code=204)
@audit.audited("user.update", object_type="user")
async def update_user(
    user_id: str,
    body: UserUpdate,
    request: Request,
    user: Claims = Depends(require_perms("user:manage")),
) -> Response:
    services = request.app.state.services
    users: dict = services.get("users", {})
    for username, rec in users.items():
        if isinstance(rec, dict) and rec.get("user_id") == user_id:
            if body.role is not None:
                rec["role"] = body.role
            if body.disabled is not None:
                rec["disabled"] = body.disabled
            return Response(status_code=204)
    raise NotFoundError(
        f"user {user_id!r} not found", context={"user_id": user_id}
    )


@router.post("/{user_id}:reset-password", status_code=204)
@audit.audited("user.reset_password", object_type="user")
async def reset_password(
    user_id: str,
    body: PasswordReset,
    request: Request,
    user: Claims = Depends(require_perms("user:manage")),
) -> Response:
    """Admin-reset a user's password (argon2 hash, never echoed).

    Updates the in-memory users store and, when a session factory is
    registered, the ``users`` DB row (best effort — an absent row only
    logs). Disabled users may be reset too: the admin can re-enable the
    account afterwards with a known password.
    """
    services = request.app.state.services
    users: dict = services.get("users", {})
    for username, rec in users.items():
        if isinstance(rec, dict) and rec.get("user_id") == user_id:
            new_hash = services["auth_service"].hash_password(body.password)
            rec["password_hash"] = new_hash
            session_factory = services.get("session_factory")
            if session_factory is not None:
                try:
                    from sqlalchemy import select as _sa_select

                    from rsi.db.tables import User

                    with session_factory() as session:
                        row = session.exec(
                            _sa_select(User).where(User.username == username)
                        ).first()
                        if row is not None:
                            row.password_hash = new_hash
                            session.add(row)
                            session.commit()
                except Exception:  # noqa: BLE001 - DB是持久化尽力而为
                    pass
            return Response(status_code=204)
    raise NotFoundError(
        f"user {user_id!r} not found", context={"user_id": user_id}
    )
