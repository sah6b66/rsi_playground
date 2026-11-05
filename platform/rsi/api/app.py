"""FastAPI application factory (DD-13 §4.1)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from rsi.api.deps import default_stores, reset_audit_log, set_current_audit_log
from rsi.api.errors import register_exception_handlers
from rsi.api.routers import ALL_ROUTERS

API_PREFIX = "/api/v1"


def create_app(services: dict[str, Any]) -> FastAPI:
    """Assemble the API app: DI container, routers, error handlers, middleware.

    ``services`` keys (P0): ``auth_service``, ``audit_log``, ``users``,
    optional ``batch_runner`` and ``stores`` (in-memory data; defaults
    created when absent).
    """
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
