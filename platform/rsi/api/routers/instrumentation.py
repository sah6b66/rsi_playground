"""Instrumentation endpoints (DD-13): batch coverage report.

B-13: the response now mirrors the DD-06 ``CoverageReport`` contract
(``covered[{aspect, method, files}]`` + ``not_instrumentable``) instead of
the line-coverage shape. No consumer of the old fields existed (grep), so
the model was replaced outright.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from rsi.api.deps import get_current_user, get_store
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/batches", tags=["instrumentation"])


class CoveredItem(BaseModel):
    aspect: str
    method: str
    files: list[str] = Field(default_factory=list)


class NotInstrumentable(BaseModel):
    aspect: str
    reason: str


class CoverageReportOut(BaseModel):
    batch_id: str
    covered: list[CoveredItem] = Field(default_factory=list)
    not_instrumentable: list[NotInstrumentable] = Field(default_factory=list)


def _get(store: dict, batch_id: str) -> dict:
    try:
        return store[batch_id]
    except KeyError:
        raise NotFoundError(
            f"batch {batch_id!r} not found", context={"batch_id": batch_id}
        ) from None


@router.get("/{batch_id}/coverage", response_model=CoverageReportOut)
async def batch_coverage(
    batch_id: str,
    user: Claims = Depends(get_current_user),
    batches: dict = Depends(get_store("batches")),
    coverage: dict = Depends(get_store("system")),
) -> CoverageReportOut:
    _get(batches, batch_id)
    rep: dict[str, Any] = (coverage.get("coverage") or {}).get(batch_id, {})
    return CoverageReportOut(
        batch_id=batch_id,
        covered=[CoveredItem(**c) for c in rep.get("covered", [])],
        not_instrumentable=[
            NotInstrumentable(**n) for n in rep.get("not_instrumentable", [])
        ],
    )
