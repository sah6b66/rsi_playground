"""FastAPI dependency injection container (DD-13 §4.2).

Services live on ``app.state.services`` (injected by :func:`rsi.api.app.create_app`);
in-memory P0 stores live on ``app.state.stores``.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer

from rsi.core.errors import ConfigError
from rsi.security.audit import AuditLog, sanitize
from rsi.security.auth import Claims
from rsi.security.rbac import require as rbac_require

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

SERVICE_KEYS = [
    "auth_service",
    "audit_log",
    "batch_runner",
    "users",
    # P0 in-memory stores registry
    "stores",
]

STORE_KEYS = ["suts", "evalsets", "profiles", "batches", "runs", "traces"]


def default_stores() -> dict[str, Any]:
    return {
        "suts": {},
        "evalsets": {},
        "profiles": {},
        "batches": {},
        "runs": {},
        "traces": {},
    }


# -- service access ---------------------------------------------------------


def get_services(request: Request) -> dict:
    return request.app.state.services


def get_service(key: str) -> Callable[[Request], Any]:
    """Dependency factory: resolve ``key`` from the DI container on app.state."""

    def dependency(request: Request) -> Any:
        try:
            return request.app.state.services[key]
        except KeyError:
            raise ConfigError(f"service {key!r} not registered") from None

    return dependency


def get_audit_log(request: Request) -> AuditLog:
    return request.app.state.services["audit_log"]


def get_store(name: str) -> Callable[[Request], dict]:
    """Dependency factory: resolve an in-memory store by name."""

    def dependency(request: Request) -> dict:
        return request.app.state.stores[name]

    return dependency


# -- authentication ---------------------------------------------------------


def get_current_user(
    request: Request, token: str = Depends(oauth2_scheme)
) -> Claims:
    auth = request.app.state.services["auth_service"]
    return auth.verify(token)


def require_perms(*perms: str) -> Callable:
    """FastAPI dependency: authenticate, then enforce RBAC via rsi.security.rbac.

    Usage: ``user: Claims = Depends(require_perms("sut:manage"))``.
    """

    checker = rbac_require(*perms)

    def dependency(user: Claims = Depends(get_current_user)) -> Claims:
        checker(user)
        return user

    return dependency


# -- audit decorator proxy ----------------------------------------------------
# The real AuditLog instance lives in the DI container, so @audited must resolve
# it lazily at request time via a ContextVar set by middleware in create_app.

_audit_log_var: ContextVar[AuditLog | None] = ContextVar(
    "rsi_audit_log", default=None
)

_ID_KWARG_NAMES = (
    "object_id",
    "id",
    "sut_id",
    "evalset_id",
    "profile_id",
    "batch_id",
    "run_id",
)


def set_current_audit_log(log: AuditLog | None) -> Any:
    return _audit_log_var.set(log)


def reset_audit_log(token: Any) -> None:
    _audit_log_var.reset(token)


class _AuditProxy:
    """Request-time @audited decorator resolving the AuditLog from ContextVar.

    Actor is taken from the handler's ``user`` kwarg (Claims.sub), else
    "system". Sensitive body fields are sanitized before recording.
    """

    def audited(
        self,
        action: str,
        object_type: str = "unknown",
        object_id: str = "",
    ) -> Callable:
        def deco(fn: Callable) -> Callable:
            sig = inspect.signature(fn)

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                # preserve the wrapped handler's signature (incl. Depends
                # defaults) so FastAPI's route analysis is unaffected
                wrapper.__signature__ = sig  # type: ignore[attr-defined]
                wrapper.__defaults__ = fn.__defaults__
                wrapper.__kwdefaults__ = fn.__kwdefaults__
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                all_kwargs = dict(bound.arguments)
                user = all_kwargs.get("user")
                actor = str(getattr(user, "sub", "system"))
                obj_id = next(
                    (
                        str(all_kwargs[k])
                        for k in _ID_KWARG_NAMES
                        if all_kwargs.get(k) is not None
                    ),
                    object_id,
                )
                body = all_kwargs.get("body")
                detail = (
                    {"body": sanitize(body.model_dump())}
                    if hasattr(body, "model_dump")
                    else {}
                )
                log = _audit_log_var.get()
                if log is not None:
                    log.record(
                        actor=actor,
                        action=action,
                        object_type=object_type,
                        object_id=obj_id,
                        detail=detail,
                    )
                result = fn(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return result

            return wrapper

        return deco


audit = _AuditProxy()


__all__ = [
    "SERVICE_KEYS",
    "STORE_KEYS",
    "audit",
    "default_stores",
    "get_audit_log",
    "get_current_user",
    "get_service",
    "get_services",
    "get_store",
    "oauth2_scheme",
    "require_perms",
    "reset_audit_log",
    "set_current_audit_log",
]