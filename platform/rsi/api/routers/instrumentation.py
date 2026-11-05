"""Instrumentation endpoints (DD-13): batch coverage report."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rsi.api.deps import get_current_user, get_store
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/batches", tags=["instrumentation"])


class CoverageFile(BaseModel):
    path: str
    lines_total: int
    lines_covered: int


class CoverageReport(BaseModel):
    batch_id: str
    lines_total: int
    lines_covered: int
    pct: float
    files: list[CoverageFile]


def _get(store: dict, batch_id: str) -> dict:
    try:
        return store[batch_id]
    except KeyError:
        raise NotFoundError(
            f"batch {batch_id!r} not found", context={"batch_id": batch_id}
        ) from None


@router.get("/{batch_id}/coverage", response_model=CoverageReport)
async def batch_coverage(
    batch_id: str,
    user: Claims = Depends(get_current_user),
    batches: dict = Depends(get_store("batches")),
    coverage: dict = Depends(get_store("system")),
) -> CoverageReport:
    _get(batches, batch_id)
    rep = (coverage.get("coverage") or {}).get(batch_id, {})
    files = [CoverageFile(**f) for f in rep.get("files", [])]
    total = rep.get("lines_total", sum(f.lines_total for f in files))
    covered = rep.get("lines_covered", sum(f.lines_covered for f in files))
    pct = round(covered / total, 4) if total else 0.0
    return CoverageReport(
        batch_id=batch_id, lines_total=total, lines_covered=covered,
        pct=pct, files=files,
    )
