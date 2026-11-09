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
from rsi.log import get_logger

logger = get_logger(__name__)

# Body-summary keys stripped recursively before audit detail is stored.
SENSITIVE_FIELDS = frozenset({"password", "secret", "token", "api_key"})

# A-11②: persisted audit detail cap (2 KiB) + truncation marker
DETAIL_JSON_MAX = 2048
TRUNCATION_MARK = "…truncated"

# Canonical audit action table (DD-03 §4). Union of the design's standard
# names and every action string used by the API layer; unknown actions are
# still recorded but flagged with a warning so the table can evolve.
AUDIT_ACTIONS: frozenset[str] = frozenset({
    # users / auth
    "user.login", "user.create", "user.update",
    "user.disable", "user.enable",
    # credentials
    "credential.put", "credential.rotate", "credential.create",
    # suts / versions
    "sut.create", "sut.update", "sut.delete",
    "sut.file_write", "sut.file_create", "sut.file_delete",
    "version.snapshot", "version.promote", "version.rollback",
    "version.files_edit",
    # evalsets
    "evalset.create", "evalset.update", "evalset.delete", "evalset.freeze",
    "evalset.assign", "evalset.import", "evalset.export",
    # batches / runs
    "batch.start", "batch.trigger", "batch.pause", "batch.resume",
    "batch.abort", "batch.rerun",
    "run.re-judge", "run.rejudge",
    # hypotheses / iterations / gates
    "hypothesis.create", "hypothesis.update", "hypothesis.submit",
    "hypothesis.submit_review", "hypothesis.apply",
    "iteration.start", "iteration.create", "iteration.pause",
    "iteration.resume", "iteration.abort", "iteration.delete",
    "iteration.archive",
    "gate.confirm_direction", "gate.hypothesis", "gate.review",
    "gate.decision",
    "decision.record",
    # labs / instances
    "lab.create", "lab.destroy", "lab.reset", "lab.snapshot", "lab.restore",
    "lab.exec_readonly",
    "instance.cleanup",
    # profiles / system / approvals
    "profile.create", "profile.update", "profile.delete",
    "system.stop", "system.stop_cancel", "system.stop_clear",
    "system.settings_update",
    "approval.approve", "approval.reject",
})


def require_known_action(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a registered audit action."""
    if name not in AUDIT_ACTIONS:
        raise ValueError(
            f"unknown audit action {name!r}; add it to AUDIT_ACTIONS "
            f"(rsi/security/audit.py)"
        )


class AuditEntry(BaseModel):
    ts: str  # UTC ISO-8601
    actor: str
    action: str
    object_type: str
    object_id: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditLog:
    """Audit trail: in-memory list (P0), plus optional ``audit_log`` table.

    When a SQLModel ``session`` or (preferred, R2-M1) a ``session_factory``
    is supplied, ``record()`` also persists each entry to the database
    (DD-03 §6); the in-memory list is always kept for backward
    compatibility. The factory form opens one short session per record —
    a long-lived shared Session would serialize (and deadlock-prone) every
    audited request onto one connection.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.entries: list[AuditEntry] = []
        self._session = session
        self._session_factory = session_factory

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
        # A-11②: cap the persisted detail at 2 KiB so a huge body (e.g. an
        # evalset import summary) cannot blow up the audit_log row.
        detail_json = json.dumps(entry.detail, default=str)
        if len(detail_json) > DETAIL_JSON_MAX:
            detail_json = detail_json[:DETAIL_JSON_MAX] + TRUNCATION_MARK
        row = AuditLogEntry(
            ts=entry.ts,
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail_json=detail_json,
        )
        if self._session_factory is not None:
            try:
                with self._session_factory() as session:
                    session.add(row)
                    session.commit()
            except Exception as exc:  # noqa: BLE001 - audit must not 500
                logger.warning(
                    "audit.db_write_failed", action=action, error=str(exc)
                )
        if self._session is not None:
            try:
                self._session.add(row)
                self._session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "audit.db_write_failed", action=action, error=str(exc)
                )

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
            if action not in AUDIT_ACTIONS:
                logger.warning(
                    "audit.unknown_action", action=action,
                    hint="add to AUDIT_ACTIONS in rsi/security/audit.py",
                )

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                all_kwargs = dict(bound.arguments)
                actor = str(all_kwargs.get("actor", "system"))
                # A-11②: record the client ip when the wrapped function
                # receives a request-like object (skipped otherwise).
                request = all_kwargs.pop("request", None)
                detail = sanitize(all_kwargs)
                ip = getattr(getattr(request, "client", None), "host", None)
                if ip:
                    detail["ip"] = ip
                self.record(
                    actor=actor,
                    action=action,
                    object_type=object_type,
                    object_id=str(all_kwargs.get("object_id", object_id)),
                    detail=detail,
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


__all__ = [
    "AUDIT_ACTIONS",
    "AuditEntry",
    "AuditLog",
    "SENSITIVE_FIELDS",
    "require_known_action",
    "sanitize",
]
