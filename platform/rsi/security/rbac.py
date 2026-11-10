"""Role-based access control (DD-03 §4.2, matrix in §6.1)."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, status

Role = Literal["admin", "agent_engineer", "researcher", "approver", "service"]
Permission = Literal[
    "sut:manage",
    "evalset:manage",
    "profile:manage",
    "batch:trigger",
    "iteration:manage",
    "version:promote_rollback",
    "approval:decide",
    "credential:manage",
    "user:manage",
    "system:stop",
    "view:read",
    # infra 资源管理（DD-14 §5.7-§5.9，T6）：admin 专属
    "image:manage",
    "sandbox:manage",
    "env:manage",
]

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "sut:manage",
            "evalset:manage",
            "profile:manage",
            "batch:trigger",
            "iteration:manage",
            "version:promote_rollback",
            "approval:decide",
            "credential:manage",
            "user:manage",
            "system:stop",
            "view:read",
            "image:manage",
            "sandbox:manage",
            "env:manage",
        }
    ),
    "agent_engineer": frozenset(
        {
            "sut:manage",
            "evalset:manage",
            "profile:manage",
            "batch:trigger",
            "iteration:manage",
            "version:promote_rollback",
            "view:read",
        }
    ),
    "researcher": frozenset({"view:read"}),
    "approver": frozenset({"view:read", "approval:decide"}),
    "service": frozenset({"batch:trigger", "view:read"}),
}


def require(*perms: str):
    """FastAPI dependency factory: caller's role must hold ALL ``perms``.

    Expects a user object with a ``role`` attribute (from auth middleware).
    """

    def dependency(user) -> object:
        role_perms = ROLE_PERMISSIONS.get(getattr(user, "role", None), frozenset())
        if not set(perms) <= role_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
            )
        return user

    return dependency


__all__ = ["Role", "Permission", "ROLE_PERMISSIONS", "require"]
