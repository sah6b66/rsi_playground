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

from rsi.core.errors import AuthError, ConfigError
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
    # Full production assembly (rsi.bootstrap.build_services; audit T2-4).
    # Routers may resolve any of these via get_service("<key>").
    "settings",
    "engine",
    "session_factory",
    "credential_store",
    "credential_resolver",
    "git_store",
    "tracing_collector",
    "trace_store",
    "prompt_library",
    "accountant",
    "llm_client",
    "llm_judge",
    "judge_service",
    "host_service",
    "host_pool",
    "net_policy",
    "sandbox_service",
    "ssh_pool",
    "stats_agg",
    "selector",
    "noise_band",
    "decision_recorder",
    "target_registry",
    "attribution_analyzer",
    "deterministic_critic",
    "change_applier",
    "sft_assessor",
    "event_bus",
    "checkpoint_store",
    "budget_controller",
    "iteration_engine",
]

STORE_KEYS = [
    "suts",
    "evalsets",
    "profiles",
    "batches",
    "runs",
    "traces",
    # Extended API stores (DD-13 Task 4/5, P0 in-memory)
    "iterations",
    "labs",
    "instances",
    "experiments",
    "hypotheses",
    "credentials",
    "settings",
    "system",
    "metrics",
]


def default_stores(settings: Any = None) -> dict[str, Any]:
    """P0 in-memory stores.

    The ``settings`` store is derived from the *real* :class:`Settings`
    instance (env > settings.yaml > defaults — H1-A) so GET
    /system/settings shows the actually-effective values. ``settings_path``
    marks entries backed by a Settings field: PUT updates the shared
    Settings object (hot-reload) and persists the leaf to settings.yaml
    (H1-B/C). Entries without it (e.g. ``sse.max_seconds``) stay
    runtime-only knobs.
    """
    from rsi.settings import Settings, get_dotted

    settings = settings or Settings()

    def derived(path: str, spath: str | None = None) -> dict[str, Any]:
        try:
            value = get_dotted(settings, spath or path)
        except (KeyError, AttributeError):
            value = None
        return {
            "value": value,
            "mutable": True,
            "source": "config",
            "settings_path": spath or path,
        }

    return {
        "suts": {},
        "evalsets": {},
        "profiles": {},
        "batches": {},
        "runs": {},
        "traces": {},
        "iterations": {},
        "labs": {},
        "instances": {},
        "experiments": {},
        "hypotheses": {},
        "credentials": {},
        "settings": {
            # real Settings-backed runtime knobs (hot-update + yaml write-back)
            "sandbox.build_retry": derived("sandbox.build_retry"),
            "sandbox.ready_timeout_s": derived("sandbox.ready_timeout_s"),
            "judge.det_timeout_s": derived("judge.det_timeout_s"),
            "judge.llm_retry": derived("judge.llm_retry"),
            "logging.level": derived("logging.level"),
            # display path maps to llm.defaults.judge.model in Settings
            "judge.llm.model": derived(
                "judge.llm.model", "llm.defaults.judge.model"
            ),
            # legacy knobs not represented in Settings (runtime-only)
            "budget.llm.daily": {"value": 100, "mutable": True},
            # static placeholder (immutable at runtime → 409)
            "platform.static_item": {"value": "static", "mutable": False},
        },
        "system": {"stopped": False, "frozen": False},
        "metrics": {},
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
    """Authenticate the request and enforce the per-request disabled check.

    Delegates to ``AuthService.get_current_user`` (DD-03 §4, ADR-19) when a
    users store is registered on the DI container; the users service maps
    username -> record, so it is re-keyed by user_id here. Without a users
    store, only JWT verification applies (backward-compatible P0 mode).
    """
    services = request.app.state.services
    auth = services["auth_service"]
    users = services.get("users") or {}
    users_store: dict[str, dict] = {}
    for username, record in users.items():
        if isinstance(record, dict) and "user_id" in record:
            users_store[record["user_id"]] = {
                "disabled": bool(record.get("disabled", False)),
                "username": record.get("username", username),
                "role": record.get("role", ""),
            }
    if users_store:
        auth.get_current_user(token, users_store)
    elif getattr(auth, "_session_factory", None) is not None:
        # D-2 (audit4): the default production assembly registers an empty
        # inline users dict — without this branch the per-request disabled
        # check would silently no-op. Fall back to the auth service's
        # DB-first disabled check (session_factory). A user that exists in
        # neither the DB nor an inline store still passes plain JWT
        # verification (backward-compatible P0 mode).
        try:
            auth.get_current_user(token, users_store)
        except AuthError as exc:
            if "disabled" in str(exc).lower():
                raise
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
                # A-11②: best-effort client ip from the FastAPI Request
                request = all_kwargs.get("request")
                ip = getattr(getattr(request, "client", None), "host", None)
                if ip:
                    detail["ip"] = ip
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