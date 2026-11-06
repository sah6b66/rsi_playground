"""Global exception handlers: RsiError -> HTTP status + ErrorResponse (DD-13 §4.3)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from rsi.core.errors import (
    AuthError,
    BudgetExceeded,
    ConflictError,
    DeviceError,
    EnvError,
    FreezeViolationError,
    InvalidTransitionError,
    LLMOutputInvalid,
    NotFastForwardError,
    NotFoundError,
    RsiError,
    TargetViolationError,
    ValidationError,
)


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: dict | None = None


# Order matters only for readability; FastAPI dispatches on exact class first
# via the registered handlers below (RsiError handler is the fallback).
EXCEPTION_MAP: dict[type[Exception], tuple[int, str]] = {
    AuthError: (401, "UNAUTHORIZED"),
    PermissionError: (403, "FORBIDDEN"),
    TargetViolationError: (403, "FORBIDDEN"),
    NotFoundError: (404, "NOT_FOUND"),
    ValidationError: (422, "VALIDATION"),
    ConflictError: (409, "CONFLICT"),
    InvalidTransitionError: (409, "CONFLICT"),
    FreezeViolationError: (409, "CONFLICT"),
    NotFastForwardError: (409, "CONFLICT"),
    DeviceError: (409, "CONFLICT"),
    EnvError: (409, "CONFLICT"),
    BudgetExceeded: (429, "BUDGET_EXCEEDED"),
    LLMOutputInvalid: (502, "LLM_INVALID"),
    RsiError: (500, "INTERNAL"),  # fallback
}


def _make_handler(status: int, code: str):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        message = str(exc)
        context = getattr(exc, "context", None) or {}
        detail = context or None
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                code=code, message=message, detail=detail
            ).model_dump(),
        )

    return handler


def register_exception_handlers(app: FastAPI) -> None:
    for exc_class, (status, code) in EXCEPTION_MAP.items():
        app.add_exception_handler(exc_class, _make_handler(status, code))


__all__ = ["EXCEPTION_MAP", "ErrorResponse", "register_exception_handlers"]
