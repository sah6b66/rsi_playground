"""System endpoints (DD-13 §5.4): health/deps/settings/stop/freeze/logs."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import ConflictError
from rsi.security.auth import Claims

router = APIRouter(tags=["system"])


class DependencyStatus(BaseModel):
    name: str
    status: str
    latency_ms: float | None = None
    # T3：down 项携带原因摘要（HealthMonitor checker 填充；fallback 恒空）
    detail: str = ""


class DependencyPage(BaseModel):
    items: list[DependencyStatus]
    # "monitor" = live HealthMonitor results; "fallback" = static defaults
    source: str = "fallback"


class SettingItem(BaseModel):
    path: str
    value: Any
    source: str
    mutable: bool
    # C1: user-tier static keys flipped since boot — a restart (POST
    # /system:restart) is required for services that snapshot the value at
    # construction time.
    pending_restart: bool = False


class SettingsPage(BaseModel):
    items: list[SettingItem]


class SettingUpdate(BaseModel):
    value: Any


# C2: LLM provider editor — llm.providers.* is excluded from GET
# /system/settings (plaintext api_key). These dedicated endpoints expose
# only a half-masked key and accept direct writes.
class LlmProviderOut(BaseModel):
    name: str
    api_key_masked: str
    base_url: str


class LlmConfigOut(BaseModel):
    providers: list[LlmProviderOut]


class LlmProviderUpdate(BaseModel):
    api_key: str | None = None
    base_url: str | None = None


def _mask_key(key: str) -> str:
    """Half-mask an api_key: keep the last 3 chars only (sk-***xxx)."""
    if not key:
        return ""
    return f"***{key[-3:]}" if len(key) > 3 else "***"


def _llm_config(request: Request) -> tuple[Any, LlmConfigOut]:
    """Resolve the live Settings object and a masked provider listing."""
    real = request.app.state.services.get("settings")
    providers: dict[str, Any] = {}
    if real is not None:
        providers = real.llm.providers
    listing = LlmConfigOut(
        providers=[
            LlmProviderOut(
                name=name,
                api_key_masked=_mask_key(
                    getattr(p, "api_key", "") if not isinstance(p, str) else p
                ),
                base_url=str(getattr(p, "base_url", "") or ""),
            )
            for name, p in providers.items()
        ]
    )
    return providers, listing


@router.get("/system/settings/llm", response_model=LlmConfigOut)
async def get_llm_config(
    request: Request,
    user: Claims = Depends(require_perms("credential:manage")),
) -> LlmConfigOut:
    """Masked LLM provider listing (C2) — never returns a full api_key."""
    _, listing = _llm_config(request)
    return listing


@router.put("/system/settings/llm/providers/{name}", response_model=LlmProviderOut)
@audit.audited("system.llm_provider_update", object_type="llm_provider")
async def update_llm_provider(
    name: str,
    body: LlmProviderUpdate,
    request: Request,
    user: Claims = Depends(require_perms("credential:manage")),
) -> LlmProviderOut:
    """Update one llm.providers.{name} entry (api_key and/or base_url).

    Writes the live Settings object and persists the whole ``llm.providers``
    section to settings.yaml (plaintext keys are accepted by design: trusted
    intranet deployment, config doc §2). The new key takes effect for
    consumers reading it lazily; a restart is recommended otherwise.
    """
    from rsi.settings import LlmProviderSettings, write_settings_section

    providers, listing = _llm_config(request)
    if name not in providers:
        raise ConflictError(
            f"unknown llm provider {name!r}", context={"name": name}
        )
    real = request.app.state.services.get("settings")
    if real is None:
        raise ConflictError("settings service unavailable", context={"name": name})
    entry = providers[name]
    if body.api_key is not None:
        entry.api_key = body.api_key
    if body.base_url is not None:
        entry.base_url = body.base_url
    write_settings_section(
        "llm.providers",
        {
            pname: LlmProviderSettings(
                api_key=pval.api_key, base_url=pval.base_url
            ).model_dump()
            for pname, pval in providers.items()
        },
    )
    return LlmProviderOut(
        name=name,
        api_key_masked=_mask_key(entry.api_key),
        base_url=entry.base_url,
    )


class StopState(BaseModel):
    stopped: bool


class FreezeState(BaseModel):
    frozen: bool


class LogEntry(BaseModel):
    model_config = {"extra": "allow"}

    ts: str
    level: str
    message: str


class LogPage(BaseModel):
    items: list[LogEntry]


_DEFAULT_DEPS = [
    {"name": "git-store", "status": "ok", "latency_ms": 1.2},
    {"name": "llm-gateway", "status": "ok", "latency_ms": 320.0},
    {"name": "sandbox", "status": "ok", "latency_ms": 5.0},
    {"name": "db", "status": "ok", "latency_ms": 0.5},
]


@router.get("/system/doctor")
async def run_doctor() -> dict:
    """Run full self-diagnostic and return structured results."""
    from rsi.doctor import Doctor

    doctor = Doctor()
    report = await doctor.run_all()
    return report.model_dump()


@router.get("/system/health")
async def health() -> dict:
    """Liveness probe (public, no auth)."""
    return {"status": "ok"}


@router.get("/system/dependencies", response_model=DependencyPage)
async def dependencies(
    request: Request,
    user: Claims = Depends(require_perms("view:read")),
) -> DependencyPage:
    monitor = request.app.state.services.get("health_monitor")
    if monitor is not None:
        items = monitor.get_cached()
        source = "monitor"
        if not items:  # first sweep not run yet: force one sweep off-loop
            # b) run the (synchronous, potentially slow) checker sweep in a
            # worker thread so the event loop / _poll_loop is not blocked.
            await asyncio.to_thread(monitor.check_all)
            items = monitor.get_cached()
    else:
        items = _DEFAULT_DEPS
        source = "fallback"
    return DependencyPage(
        items=[
            DependencyStatus(
                name=d["name"],
                status=d["status"],
                latency_ms=d.get("latency_ms"),
                detail=str(d.get("detail") or ""),
            )
            for d in items
        ],
        source=source,
    )


@router.get("/system/settings", response_model=SettingsPage)
async def get_settings(
    user: Claims = Depends(require_perms("credential:manage")),
    settings: dict = Depends(get_store("settings")),
) -> SettingsPage:
    return SettingsPage(
        items=[
            SettingItem(path=p, value=v["value"],
                        source=v.get("source", "runtime"),
                        mutable=v.get("mutable", False),
                        pending_restart=bool(v.get("pending_restart", False)))
            for p, v in settings.items()
        ]
    )


@router.put("/system/settings/{path:path}", response_model=SettingItem)
@audit.audited("system.settings_update", object_type="setting")
async def update_setting(
    path: str,
    body: SettingUpdate,
    request: Request,
    user: Claims = Depends(require_perms("credential:manage")),
    settings: dict = Depends(get_store("settings")),
) -> SettingItem:
    """Update a mutable setting.

    H1-B/C: for Settings-backed entries (``settings_path`` present) the
    shared ``services["settings"]`` object is updated in place — consumers
    that lazily read ``settings.<field>`` per request/batch (routers hosts
    / images, BatchRunner per-batch timeouts, HostService quotas) see the
    new value immediately; services that snapshotted fields at
    construction (SandboxService.build_retry, JudgeService det timeout,
    HealthMonitor interval, ...) take effect after restart, and the value
    is persisted to settings.yaml (env vars still win over yaml on
    reload). Store-only knobs (no ``settings_path``) remain runtime-only.
    Invalid values are rejected with 422; static entries with 409.
    """
    if path not in settings:
        raise ConflictError(
            f"unknown setting {path!r}", context={"path": path}
        )
    item = settings[path]
    if not item.get("mutable", False):
        raise ConflictError(
            f"setting {path!r} is static and cannot be changed at runtime",
            context={"path": path},
        )
    spath = item.get("settings_path")
    if not spath:
        # runtime-only knob (not represented in Settings/yaml)
        item["value"] = body.value
        return SettingItem(path=path, value=item["value"], source="runtime",
                           mutable=True)
    real = request.app.state.services.get("settings")
    value = body.value
    if real is not None:
        from pydantic import ValidationError as PydanticValidationError

        from rsi.core.errors import ValidationError
        from rsi.settings import set_dotted, write_settings_section

        try:
            value = set_dotted(real, spath, body.value)
        except PydanticValidationError as exc:
            raise ValidationError(
                f"invalid value for {path!r}: {exc.error_count()} error(s)",
                context={"path": path},
            ) from exc
        except (KeyError, AttributeError) as exc:
            raise ConflictError(
                f"setting {path!r} has no backing Settings field",
                context={"path": path},
            ) from exc
        # persist the leaf key to settings.yaml (atomic rewrite) and mark
        # static (non-hot-reload) keys as pending-restart (C1)
        write_settings_section(spath, value)
        if item.get("restart_required", True):
            item["pending_restart"] = True
    item["value"] = value
    return SettingItem(path=path, value=item["value"], source="yaml+runtime",
                       mutable=True, pending_restart=bool(item.get("pending_restart")))


@router.post("/system:stop", response_model=StopState)
@audit.audited("system.stop", object_type="system")
async def stop_on(
    request: Request,
    user: Claims = Depends(require_perms("system:stop")),
    system: dict = Depends(get_store("system")),
) -> StopState:
    system["stopped"] = True
    # D-5: a real STOP must create the BudgetController stop file so
    # BatchRunner aborts; without the controller we only flip the flag.
    budget = request.app.state.services.get("budget_controller")
    if budget is not None:
        budget.stop_file.parent.mkdir(parents=True, exist_ok=True)
        budget.stop_file.write_text(
            "manual STOP via POST /system:stop\n", encoding="utf-8"
        )
    return StopState(stopped=True)


@router.post("/system:restart", status_code=202)
@audit.audited("system.restart", object_type="system")
async def restart(
    request: Request,
    user: Claims = Depends(require_perms("system:stop")),
) -> dict:
    """Restart the platform process (C1): settings.yaml edits flagged
    ``pending_restart`` only take effect after this.

    write_settings_section is synchronous-atomic, so there is no async
    flush to await. We log, answer 202, then hard-exit after a 1 s delay
    (threading.Timer) so the response reaches the client first; the
    supervisor (systemd / docker restart policy) brings the process back.
    """
    from rsi.log import get_logger

    get_logger(__name__).info("restart.requested", actor=str(user.sub))
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"status": "restarting"}


@router.delete("/system:stop", response_model=StopState)
@audit.audited("system.stop_cancel", object_type="system")
async def stop_off(
    request: Request,
    user: Claims = Depends(require_perms("system:stop")),
    system: dict = Depends(get_store("system")),
) -> StopState:
    system["stopped"] = False
    # D-5: recovery must delete the BudgetController stop file too, else
    # BatchRunner keeps aborting despite the API reporting "recovered".
    budget = request.app.state.services.get("budget_controller")
    if budget is not None:
        try:
            budget.stop_file.unlink(missing_ok=True)
        except OSError:
            pass
    return StopState(stopped=False)


@router.get("/system/freeze", response_model=FreezeState)
async def freeze_status(
    user: Claims = Depends(require_perms("view:read")),
    system: dict = Depends(get_store("system")),
) -> FreezeState:
    return FreezeState(frozen=bool(system.get("frozen", False)))


@router.get("/system/logs", response_model=LogPage)
async def query_logs(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    level: str | None = None,
    user: Claims = Depends(require_perms("view:read")),
    system: dict = Depends(get_store("system")),
) -> LogPage:
    logs: list[dict] = system.setdefault(
        "logs",
        [{"ts": "2026-01-01T00:00:00Z", "level": "INFO", "message": "boot"}],
    )
    if level is not None:
        logs = [e for e in logs if e["level"].lower() == level.lower()]
    items, page = paginate(logs, cursor, limit)
    return LogPage(items=[LogEntry(**e) for e in items])
