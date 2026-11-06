"""FastAPI application factory (DD-13 §4.1)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from rsi.api.deps import default_stores, reset_audit_log, set_current_audit_log
from rsi.api.errors import register_exception_handlers
from rsi.api.routers import ALL_ROUTERS
from rsi.log import get_logger

API_PREFIX = "/api/v1"

logger = get_logger(__name__)


def create_app(
    services: dict[str, Any] | None = None, run_doctor: bool = False
) -> FastAPI:
    """Assemble the API app: DI container, routers, error handlers, middleware.

    ``services`` keys (P0): ``auth_service``, ``audit_log``, ``users``,
    optional ``batch_runner`` and ``stores`` (in-memory data; defaults
    created when absent). When ``services`` is None the full production
    assembly from :func:`rsi.bootstrap.build_services` is used (offline-safe
    Fake/mock defaults; explicit ``services`` always win — audit T2-4).

    If ``run_doctor`` is true, run the startup self-diagnostic first,
    print the report and abort startup on blocking errors.
    """
    if run_doctor:
        from rsi.doctor import Doctor

        doctor = Doctor()
        report = asyncio.run(doctor.run_all())
        doctor.print_report(report)
        if report.has_errors:
            raise SystemExit(
                "启动中止：自检存在阻塞项 (fail)，请修复后重试。"
                "可运行 python -m rsi.doctor 查看详情。"
            )
    if services is None:
        from rsi.bootstrap import build_services

        services = build_services()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "api.assembly",
            services=sorted(services.keys()),
            default_assembly=True,
        )
        # startup: HealthMonitor polling (D-1, DD-12 §4.7) — one sweep
        # immediately so /system/dependencies has data, then interval polls.
        monitor = services.get("health_monitor")
        if monitor is not None and hasattr(monitor, "start"):
            try:
                await asyncio.to_thread(monitor.check_all)
            except Exception:  # noqa: BLE001 - first sweep is best-effort
                pass
            await monitor.start()
        # startup: STOP-file polling (D-5) — in-process safety net calling
        # budget.check_global() every 30s; a breach writes the STOP file +
        # publishes the alert (inside check_global) even without batches.
        stop_task: asyncio.Task | None = None
        budget = services.get("budget_controller")
        if budget is not None and hasattr(budget, "check_global"):

            async def _stop_poll() -> None:
                cfg = services.get("settings")
                interval = (
                    float(cfg.orchestrator.health_monitor_interval_s)
                    if cfg is not None else 30.0
                )
                while True:
                    try:
                        await budget.check_global()
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - poller must not die
                        pass  # BudgetExceeded already wrote STOP + alerted
                    await asyncio.sleep(interval)

            stop_task = asyncio.create_task(_stop_poll(), name="stop-polling")
        yield
        # shutdown: stop background pollers, then reclaim pooled resources
        if stop_task is not None:
            stop_task.cancel()
            # c) await the cancelled task so the poller actually finishes
            # (and its finally-blocks run) before the loop closes.
            await asyncio.gather(stop_task, return_exceptions=True)
        if monitor is not None and hasattr(monitor, "stop"):
            try:
                await monitor.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "api.shutdown", component="health_monitor",
                    exc_type=type(exc).__name__,
                )
        sandbox = services.get("sandbox_service")
        if sandbox is not None and hasattr(sandbox, "close"):
            try:
                sandbox.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("api.shutdown", component="sandbox", exc_type=type(exc).__name__)

    app = FastAPI(
        title="RSI Platform API",
        version="0.1.0",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.stores = services.get("stores") or default_stores()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-pagination"],
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def _audit_context(request: Request, call_next):
        token = set_current_audit_log(services.get("audit_log"))
        try:
            return await call_next(request)
        finally:
            reset_audit_log(token)

    for router in ALL_ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


__all__ = ["API_PREFIX", "create_app"]
