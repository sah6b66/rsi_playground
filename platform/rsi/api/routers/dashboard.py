"""Dashboard endpoints (DD-13): metrics series / model compare / defect dist."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
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


def _all_batches(request: Request, store: dict) -> list[dict]:
    """内存批记录 + DB 回退（重启后内存空），按创建时间排序。"""
    from rsi.api.db_reads import _session_factory

    out: dict[str, dict] = {b["id"]: b for b in store.values()}
    sf = _session_factory(request)
    if sf is not None:
        try:
            from sqlmodel import select

            from rsi.db.tables import Batch

            with sf() as session:  # type: ignore[misc]
                for row in session.exec(select(Batch)).all():
                    out.setdefault(
                        row.id,
                        {
                            "id": row.id,
                            "evalset_id": row.evalset_id,
                            "sut_id": row.sut_id,
                            "purpose": row.purpose,
                            "status": row.status or "",
                            "created_at": row.created_at,
                            "iteration_id": row.iteration_id,
                        },
                    )
        except Exception:  # noqa: BLE001 - 读回退不得 500
            pass
    return sorted(out.values(), key=lambda b: b.get("created_at", ""))


def _batch_runs(request: Request, runs_store: dict, batch_id: str) -> list[dict]:
    from rsi.api.db_reads import list_runs_for_batch

    return list_runs_for_batch(request, runs_store, batch_id)


def _run_score(run: dict, key: str) -> float | None:
    result = run.get("result")
    if not isinstance(result, dict):
        return None
    scores = result.get("scores")
    if not isinstance(scores, dict):
        return None
    v = scores.get(key)
    return float(v) if isinstance(v, (int, float)) else None


@router.get("/metrics", response_model=MetricsOut)
async def metrics(
    request: Request,
    metric: str = "success_rate",
    sut_id: str | None = None,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
    batches: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
) -> MetricsOut:
    """F2/W1：按批聚合真实 runs（success_rate / root_cause_accuracy）。

    显式写入 metrics store 的序列（测试/运营注入）优先；无批数据时返回
    空序列（不再回退 demo 假数据）。
    """
    if store.get(metric):
        return MetricsOut(metric=metric, series=[SeriesPoint(**p) for p in store[metric]])
    key = "task_complete" if metric == "success_rate" else "root_cause_accuracy"
    series: list[SeriesPoint] = []
    for batch in _all_batches(request, batches):
        if sut_id and batch.get("sut_id") != sut_id:
            continue
        batch_runs = _batch_runs(request, runs, batch["id"])
        vals = [v for r in batch_runs if (v := _run_score(r, key)) is not None]
        if not vals:
            continue
        value = (
            sum(1.0 for v in vals if v >= 1.0) / len(vals)
            if metric == "success_rate"
            else sum(vals) / len(vals)
        )
        series.append(SeriesPoint(ts=batch.get("created_at", ""), value=value))
    return MetricsOut(metric=metric, series=series)


@router.get("/model-compare", response_model=ModelCompareOut)
async def model_compare(
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
    batches: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
    suts: dict = Depends(get_store("suts")),
) -> ModelCompareOut:
    if store.get("model_compare"):
        return ModelCompareOut(items=[ModelScore(**m) for m in store["model_compare"]])
    agg: dict[str, list[float]] = {}
    for batch in _all_batches(request, batches):
        sut = batch.get("sut_id") or ""
        for run in _batch_runs(request, runs, batch["id"]):
            v = _run_score(run, "task_complete")
            if v is not None:
                agg.setdefault(sut, []).append(v)
    items = [
        ModelScore(
            model=str((suts.get(sut) or {}).get("name") or sut),
            sut_id=sut or None,
            score=sum(v) / len(v),
        )
        for sut, v in sorted(agg.items())
        if v
    ]
    return ModelCompareOut(items=items)


@router.get("/defect-dist", response_model=DefectDistOut)
async def defect_dist(
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("metrics")),
) -> DefectDistOut:
    dist = store.get("defect_dist") or {"a": {}, "b": {}}
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
    """噪声带标定状态（ADR-17，§9.17）。

    D-8: prefer persisted ``noise_bands`` rows ( orchestrated calibration
    writes them); fall back to the in-process calibrator's last result.
    """
    db_items = _noise_bands_from_db(request, evalset_id)
    if db_items is not None:
        return NoiseBandsOut(items=db_items)
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


class CalibrateBody(BaseModel):
    sut_id: str = Field(json_schema_extra={"example": "sut-1a2b3c4d"})
    evalset_id: str = Field(json_schema_extra={"example": "es-abc12345"})
    k: int = Field(default=3, ge=2, le=10)


class CalibrateOut(BaseModel):
    metrics: dict[str, dict[str, float]]
    k: int
    evalset_hash: str
    low_confidence: bool
    valid: bool
    n_pairs: int


@router.post("/noise-bands:calibrate", response_model=CalibrateOut)
@audit.audited("noise_band.calibrate", object_type="evalset")
async def calibrate_noise_bands(
    body: CalibrateBody,
    request: Request,
    user: Claims = Depends(require_perms("iteration:manage")),
    evalsets: dict = Depends(get_store("evalsets")),
) -> CalibrateOut:
    """显式噪声带标定（ADR-17 / DD-10 §5）。

    文档（interfaces §9.17）只定义了 ``GET /dashboard/noise-bands`` 的读取
    契约，未定义标定触发端点；实现取 ``POST /dashboard/noise-bands:calibrate``
    与读取端点同族（同前缀）。权限 ``iteration:manage``（标定会烧预算并
    影响后续 promote 决策，与 A-14 迭代管理端点同族）。

    同步语义：离线/伪执行下标定很快，端点同步执行 k 轮重复批并 200 返回
    ``NoiseBandInfo``（结果同时由 calibrator 落库至 ``noise_bands`` 表，
    ``GET /dashboard/noise-bands?evalset_id=`` 即可读取）。
    """
    es = evalsets.get(body.evalset_id)
    if es is None:
        from rsi.core.errors import NotFoundError

        raise NotFoundError(
            f"evalset {body.evalset_id!r} not found",
            context={"evalset_id": body.evalset_id},
        )
    calibrator = request.app.state.services.get("noise_band")
    if calibrator is None:
        from rsi.core.errors import ConfigError

        raise ConfigError("noise_band calibrator not registered")
    factory = request.app.state.services.get("noise_run_batch_fn")
    if factory is None:
        from rsi.core.errors import ConfigError

        raise ConfigError("noise_run_batch_fn not registered")
    task_index = {str(t["id"]): t for t in es.get("tasks") or [] if "id" in t}
    if not task_index:
        from rsi.core.errors import ValidationError as RsiValidationError

        raise RsiValidationError(
            f"evalset {body.evalset_id!r} has no tasks to calibrate",
            context={"evalset_id": body.evalset_id},
        )
    run_batch_fn = factory(body.evalset_id, body.sut_id)
    info = await calibrator.calibrate(
        body.sut_id,
        evalset_id=body.evalset_id,
        k=body.k,
        run_batch_fn=run_batch_fn,
        task_index=task_index,
    )
    return CalibrateOut(
        metrics=info.metrics,
        k=info.k,
        evalset_hash=info.evalset_hash,
        low_confidence=info.low_confidence,
        valid=info.valid,
        n_pairs=info.n_pairs,
    )


def _noise_bands_from_db(
    request: Request, evalset_id: str | None
) -> list[NoiseBandItem] | None:
    """Read persisted noise_bands rows; None when no DB/session is wired."""
    session_factory = request.app.state.services.get("session_factory")
    if session_factory is None:
        return None
    try:
        from rsi.db.tables import NoiseBand
    except Exception:  # pragma: no cover - import guard
        return None
    with session_factory() as session:
        query = session.query(NoiseBand)
        if evalset_id:
            query = query.filter(NoiseBand.evalset_id == evalset_id)
        rows = query.order_by(NoiseBand.evalset_id, NoiseBand.metric).all()
    if not rows:
        return None
    out: list[NoiseBandItem] = []
    for row in rows:
        n_pairs = int(getattr(row, "n_pairs", row.k) or row.k)
        low_conf = bool(row.low_confidence)
        out.append(
            NoiseBandItem(
                metric=row.metric,
                sigma=float(row.sigma),
                delta=float(row.delta),
                k=int(row.k) or None,
                evalset_hash=row.evalset_hash,
                low_confidence=low_conf,
                calibrated_at=row.calibrated_at,
                valid=(not low_conf) and n_pairs >= 2,
            )
        )
    return out
