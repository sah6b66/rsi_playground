"""Experiment endpoints (DD-13): list / detail / version comparison."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rsi.api.deps import get_current_user, get_store
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/experiments", tags=["experiments"])


class ExperimentOut(BaseModel):
    id: str
    sut_id: str
    iteration_id: str | None
    versions: dict[str, Any]
    decision: str
    evidence: list[dict[str, Any]]


class ExperimentPage(BaseModel):
    items: list[ExperimentOut]


class CompareOut(BaseModel):
    experiment_id: str
    a: dict[str, Any]
    b: dict[str, Any]
    diff: dict[str, Any]


def _get(store: dict, experiment_id: str) -> dict:
    try:
        return store[experiment_id]
    except KeyError:
        raise NotFoundError(
            f"experiment {experiment_id!r} not found",
            context={"experiment_id": experiment_id},
        ) from None


@router.get("", response_model=ExperimentPage)
async def list_experiments(
    sut_id: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("experiments")),
) -> ExperimentPage:
    items = [e for e in store.values() if sut_id is None or e["sut_id"] == sut_id]
    page_items, _page = paginate(items, cursor, limit)
    return ExperimentPage(items=[ExperimentOut(**e) for e in page_items])


@router.get("/{experiment_id}", response_model=ExperimentOut)
async def get_experiment(
    experiment_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("experiments")),
) -> ExperimentOut:
    return ExperimentOut(**_get(store, experiment_id))


@router.get("/{experiment_id}/compare", response_model=CompareOut)
async def compare_experiment(
    experiment_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("experiments")),
) -> CompareOut:
    exp = _get(store, experiment_id)
    versions = exp.get("versions", {})
    a, b = versions.get("a", {}), versions.get("b", {})
    keys = set(a) | set(b)
    diff = {
        k: {"a": a.get(k), "b": b.get(k)}
        for k in sorted(keys)
        if a.get(k) != b.get(k)
    }
    return CompareOut(experiment_id=experiment_id, a=a, b=b, diff=diff)
