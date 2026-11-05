"""Judgment endpoints (DD-13 §9.7): run verdict + pairwise comparison."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rsi.api.deps import get_current_user, get_store
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

runs_router = APIRouter(prefix="/runs", tags=["judgments"])
router = APIRouter(tags=["judgments"])


def _get_run(store: dict, run_id: str) -> dict:
    try:
        return store[run_id]
    except KeyError:
        raise NotFoundError(
            f"run {run_id!r} not found", context={"run_id": run_id}
        ) from None


class VerdictOut(BaseModel):
    run_id: str
    verdict: str
    score: float
    reasons: list[str]
    dimensions: dict[str, Any]


class PairwiseBody(BaseModel):
    run_a: str
    run_b: str


class PairwiseOut(BaseModel):
    run_a: str
    run_b: str
    winner: str  # run_a | run_b | tie
    reasons: list[str]


@runs_router.get("/{run_id}/judgment", response_model=VerdictOut)
async def get_run_judgment(
    run_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
) -> VerdictOut:
    run = _get_run(store, run_id)
    v = run.get("verdict") or {}
    return VerdictOut(
        run_id=run_id,
        verdict=v.get("verdict", "pending"),
        score=v.get("score", 0.0),
        reasons=v.get("reasons", []),
        dimensions=v.get("dimensions", {}),
    )


@router.post("/judgments:pairwise", response_model=PairwiseOut)
async def pairwise_judgment(
    body: PairwiseBody,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
) -> PairwiseOut:
    a = _get_run(store, body.run_a)
    b = _get_run(store, body.run_b)
    sa = (a.get("verdict") or {}).get("score", 0.0)
    sb = (b.get("verdict") or {}).get("score", 0.0)
    if sa > sb:
        winner, reason = body.run_a, f"score {sa} > {sb}"
    elif sb > sa:
        winner, reason = body.run_b, f"score {sb} > {sa}"
    else:
        winner, reason = "tie", f"equal scores {sa}"
    return PairwiseOut(
        run_a=body.run_a, run_b=body.run_b, winner=winner, reasons=[reason],
    )
