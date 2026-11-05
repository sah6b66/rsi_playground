"""Dashboard endpoints (DD-13): metrics series / model compare / defect dist."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rsi.api.deps import get_current_user, get_store
from rsi.security.auth import Claims

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class SeriesPoint(BaseModel):
    ts: str
    value: float


class MetricsOut(BaseModel):
    metric: str
    series: list[SeriesPoint]


class ModelScore(BaseModel):
    model: str
    sut_id: str | None
    score: float


class ModelCompareOut(BaseModel):
    items: list[ModelScore]


class DefectDistOut(BaseModel):
    a: dict[str, int]
    b: dict[str, int]


_DEFAULT_SERIES = [
    ("2026-11-01T00:00:00Z", 0.5),
    ("2026-11-01T06:00:00Z", 0.55),
    ("2026-11-01T12:00:00Z", 0.62),
    ("2026-11-02T00:00:00Z", 0.7),
]


@router.get("/metrics", response_model=MetricsOut)
async def metrics(
    metric: str = "success_rate",
    sut_id: str | None = None,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
) -> MetricsOut:
    series = store.get(metric) or [
        {"ts": ts, "value": v} for ts, v in _DEFAULT_SERIES
    ]
    return MetricsOut(metric=metric, series=[SeriesPoint(**p) for p in series])


@router.get("/model-compare", response_model=ModelCompareOut)
async def model_compare(
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
) -> ModelCompareOut:
    items = store.get("model_compare") or [
        {"model": "glm-4", "sut_id": None, "score": 0.71},
        {"model": "gpt-x", "sut_id": None, "score": 0.68},
    ]
    return ModelCompareOut(items=[ModelScore(**m) for m in items])


@router.get("/defect-dist", response_model=DefectDistOut)
async def defect_dist(
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
) -> DefectDistOut:
    dist = store.get("defect_dist") or {
        "a": {"tool_error": 12, "timeout": 5, "hallucination": 3},
        "b": {"tool_error": 7, "timeout": 6, "hallucination": 1},
    }
    a = dict(dist.get("a", {}))
    b = dict(dist.get("b", {}))
    return DefectDistOut(a=a, b=b)
