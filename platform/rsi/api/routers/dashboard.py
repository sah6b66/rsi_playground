"""Dashboard endpoints (DD-13): metrics series / model compare / defect dist."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
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


class NoiseBandItem(BaseModel):
    metric: str
    sigma: float
    delta: float
    k: int | None = None
    evalset_hash: str | None = None
    low_confidence: bool = False
    calibrated_at: str | None = None
    valid: bool = True


class NoiseBandsOut(BaseModel):
    items: list[NoiseBandItem]


@router.get("/noise-bands", response_model=NoiseBandsOut)
async def noise_bands(
    request: Request,
    evalset_id: str | None = None,
    user: Claims = Depends(get_current_user),
) -> NoiseBandsOut:
    """噪声带标定状态（ADR-17，§9.17）。读 NoiseBandCalibrator 最近一次标定。"""
    calibrator = request.app.state.services.get("noise_band")
    items: list[NoiseBandItem] = []
    if calibrator is not None:
        factor = float(getattr(calibrator, "factor", 1.96))
        sigma_map: dict = getattr(calibrator, "last_sigma", {}) or {}
        n_pairs = int(getattr(calibrator, "last_n_pairs", 0) or 0)
        # 文档未定义 calibrated_at 来源，实现取标定器最近标定时间（无则空）
        calibrated_at = getattr(calibrator, "last_calibrated_at", None)
        for metric, sigma in sorted(sigma_map.items()):
            items.append(
                NoiseBandItem(
                    metric=metric,
                    sigma=float(sigma),
                    delta=factor * float(sigma),
                    k=n_pairs or None,
                    evalset_hash=None,
                    low_confidence=(n_pairs or 0) < 3,
                    calibrated_at=calibrated_at,
                    valid=True,
                )
            )
    return NoiseBandsOut(items=items)
