"""Audit log query endpoint (DD-13). Admin-only read access to AuditLog."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import get_audit_log, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.security.auth import Claims

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


class AuditEntryOut(BaseModel):
    ts: str
    actor: str
    action: str
    object_type: str
    object_id: str
    detail: dict


class AuditPage(BaseModel):
    items: list[AuditEntryOut]


@router.get("", response_model=AuditPage)
async def query_audit_logs(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    action: str | None = None,
    actor: str | None = None,
    user: Claims = Depends(require_perms("user:manage")),
    log=Depends(get_audit_log),
) -> AuditPage:
    entries = [e.model_dump() for e in log.entries]
    if action is not None:
        entries = [e for e in entries if action in e["action"]]
    if actor is not None:
        entries = [e for e in entries if e["actor"] == actor]
    items, page = paginate(entries, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return AuditPage(items=[AuditEntryOut(**e) for e in items])
