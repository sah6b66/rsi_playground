"""Audit log + @audited decorator (DD-03 §4).

P0 foundation: entries live in an in-memory list; DB persistence
(audit_log table, no aging, NFR7 backup) arrives with the DB integration
slice.
"""

from __future__ import annotations

import functools
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlmodel import Session

from rsi.db.tables import AuditLogEntry

# Body-summary keys stripped recursively before audit detail is stored.
SENSITIVE_FIELDS = frozenset({"password", "secret", "token", "api_key"})


class AuditEntry(BaseModel):
    ts: str  # UTC ISO-8601
    actor: str
    action: str
    object_type: str
    object_id: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditLog:
    """Audit trail: in-memory list (P0), plus optional ``audit_log`` table.

    When an optional SQLModel ``session`` is supplied, ``record()`` also
    persists each entry to the database (DD-03 §6); the in-memory list is
    always kept for backward compatibility.
    """

    def __init__(self, session: Session | None = None) -> None:
        self.entries: list[AuditEntry] = []
        self._session = session

    def record(
        self,
        actor: str,
        action: str,
        object_type: str,
        object_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditEntry(
            ts=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail or {},
        )
        self.entries.append(entry)
        if self._session is not None:
            self._session.add(
                AuditLogEntry(
                    ts=entry.ts,
                    actor=actor,
                    action=action,
                    object_type=object_type,
                    object_id=object_id,
                    detail_json=json.dumps(entry.detail, default=str),
                )
            )
            self._session.commit()

    def audited(
        self,
        action: str,
        object_type: str = "unknown",
        object_id: str = "",
    ) -> Callable:
        """Decorator: log a call with a sanitized kwargs summary.

        Actor is taken from the wrapped function's ``actor`` kwarg when
        present, else "system". Values under SENSITIVE_FIELDS keys are
        replaced with "***" (recursively for nested dicts).
        """

        def deco(fn: Callable) -> Callable:
            sig = inspect.signature(fn)

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                all_kwargs = dict(bound.arguments)
                actor = str(all_kwargs.get("actor", "system"))
                self.record(
                    actor=actor,
                    action=action,
                    object_type=object_type,
                    object_id=str(all_kwargs.get("object_id", object_id)),
                    detail=sanitize(all_kwargs),
                )
                return fn(*args, **kwargs)

            return wrapper

        return deco


def sanitize(value: Any) -> Any:
    """Recursively replace sensitive field values with '***'."""
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in SENSITIVE_FIELDS else sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


__all__ = ["AuditEntry", "AuditLog", "SENSITIVE_FIELDS", "sanitize"]
