"""FastAPI application factory (DD-13 §4.1)."""

from __future__ import annotations

from typing import Any

import asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from rsi.api.deps import default_stores, reset_audit_log, set_current_audit_log
from rsi.api.errors import register_exception_handlers
from rsi.api.routers import ALL_ROUTERS

API_PREFIX = "/api/v1"


def create_app(services: dict[str, Any], run_doctor: bool = False) -> FastAPI:
    """Assemble the API app: DI container, routers, error handlers, middleware.

    ``services`` keys (P0): ``auth_service``, ``audit_log``, ``users``,
    optional ``batch_runner`` and ``stores`` (in-memory data; defaults
    created when absent).

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
    app = FastAPI(
        title="RSI Platform API",
        version="0.1.0",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
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
