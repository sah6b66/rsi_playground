"""System endpoints (DD-13 §5.4): health/deps/settings/stop/freeze/logs."""

from __future__ import annotations

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


class DependencyPage(BaseModel):
    items: list[DependencyStatus]
    # "monitor" = live HealthMonitor results; "fallback" = static defaults
    source: str = "fallback"


class SettingItem(BaseModel):
    path: str
    value: Any
    source: str
    mutable: bool


class SettingsPage(BaseModel):
    items: list[SettingItem]


class SettingUpdate(BaseModel):
    value: Any


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
        if not items:  # first sweep not run yet: force one synchronous sweep
            monitor.check_all()
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
            SettingItem(path=p, value=v["value"], source="runtime",
                        mutable=v.get("mutable", False))
            for p, v in settings.items()
        ]
    )


@router.put("/system/settings/{path:path}", response_model=SettingItem)
@audit.audited("system.settings_update", object_type="setting")
async def update_setting(
    path: str,
    body: SettingUpdate,
    user: Claims = Depends(require_perms("credential:manage")),
    settings: dict = Depends(get_store("settings")),
) -> SettingItem:
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
    item["value"] = body.value
    return SettingItem(path=path, value=item["value"], source="runtime",
                       mutable=True)


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
