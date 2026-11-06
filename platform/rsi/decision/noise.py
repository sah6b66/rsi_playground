"""NoiseBandCalibrator — ADR-17 explicit calibration batch (DD-10 §4/§5).

Two modes (D-8):

* **Orchestrated** — ``calibrate(sut_id, evalset_id, k=3, run_batch_fn=...,
  task_index=...)``: stratified-samples up to 10 tasks (grouped by the
  task-id domain prefix ``task_id.split('/')[0]``, ADR-17), re-runs them
  ``k`` times through ``run_batch_fn`` (same config, bypass_cache) and
  derives per-metric σ / δ = factor × σ from the per-task score pairs.
  Persists one ``noise_bands`` row per metric when a session factory is
  wired.
* **Legacy (pure function)** — ``calibrate(batch_stats_runs)`` consumes
  pre-computed same-config batch statistics; σ is the population standard
  deviation of each metric across the runs. Kept for backward compatibility
  with existing callers/tests.

``NoiseBandInfo`` follows the DD-10 §3 shape: per-metric
{sigma, delta, n_pairs} plus k / evalset_hash / low_confidence / valid.
``low_confidence`` means fewer than half of the sampled tasks produced
usable pairs; ``valid`` requires ``not low_confidence and n_pairs >= 2``
(P2 auto-promotion precondition, ADR-17).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from rsi.core.models import BatchStats, NoiseBandInfo
from rsi.log import get_logger

__all__ = ["NoiseBandCalibrator"]

logger = get_logger(__name__)

_FACTOR_95 = 1.96
_STRATIFIED_SAMPLE_MAX = 10  # ADR-17: min(10, task count)


def _stdev(values: list[float]) -> float:
    """Population standard deviation of *values* (len >= 1)."""
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / n
    return var**0.5


def _hash_task_ids(task_ids: list[str]) -> str:
    return hashlib.sha1("\n".join(sorted(task_ids)).encode()).hexdigest()[:16]


def stratified_sample(task_ids: list[str], limit: int = _STRATIFIED_SAMPLE_MAX) -> list[str]:
    """ADR-17 stratified sampling: group by task-id domain prefix
    (``task_id.split('/')[0]``), then pick round-robin across groups until
    ``min(limit, total)`` tasks are selected (even per-group coverage)."""
    groups: dict[str, list[str]] = {}
    for tid in task_ids:
        groups.setdefault(tid.split("/")[0], []).append(tid)
    total = min(limit, len(task_ids))
    picked: list[str] = []
    iters = {k: 0 for k in groups}
    while len(picked) < total:
        progressed = False
        for key in sorted(groups):
            idx = iters[key]
            if idx < len(groups[key]):
                picked.append(groups[key][idx])
                iters[key] = idx + 1
                progressed = True
                if len(picked) >= total:
                    break
        if not progressed:
            break
    return picked


class NoiseBandCalibrator:
    """Calibrate the per-metric noise band from explicit repeat batches."""

    def __init__(self, factor: float = _FACTOR_95, session_factory=None) -> None:
        self.factor = factor
        self._session_factory = session_factory
        # Last calibration's raw per-metric sigma (evidence, not in the band model).
        self.last_sigma: dict[str, float] = {}
        # API (§9.17) needs the last calibration metadata as well.
        self.last_n_pairs: int = 0
        self.last_calibrated_at: str | None = None
        self.last_evalset_id: str | None = None
        self.last_evalset_hash: str | None = None
        self.last_low_confidence: bool = False
        self.last_valid: bool = False

    # -- entry point ---------------------------------------------------------

    def calibrate(self, sut_id, evalset_id=None, k: int = 3, run_batch_fn=None, task_index=None):
        """Dispatch: legacy list-of-BatchStats -> sync result; orchestrated
        (sut_id/evalset_id strings) -> awaitable."""
        if isinstance(sut_id, list):
            return self._calibrate_from_stats(sut_id, k=k)
        return self._calibrate_orchestrated(
            sut_id, evalset_id, k=k, run_batch_fn=run_batch_fn, task_index=task_index
        )

    # -- orchestrated mode (D-8) ---------------------------------------------

    async def _calibrate_orchestrated(
        self,
        sut_id: str,
        evalset_id: str | None,
        k: int = 3,
        run_batch_fn=None,
        task_index=None,
    ) -> NoiseBandInfo:
        """Stratified-sample tasks, run them k times via *run_batch_fn*
        (same config, bypass_cache — ADR-17) and derive the band.

        ``task_index`` maps task_id -> task descriptor (or is a plain
        collection of task ids); only its keys/elements define the sampling
        domain. ``run_batch_fn(task_ids) -> dict[task_id, dict[metric, value]]``
        must execute one pass over exactly those tasks.
        """
        if run_batch_fn is None:
            raise ValueError(
                "orchestrated calibration requires run_batch_fn "
                "(or use the legacy calibrate(batch_stats_runs) form)"
            )
        if task_index is None:
            raise ValueError("orchestrated calibration requires task_index")
        if isinstance(task_index, dict):
            all_task_ids = list(task_index.keys())
        else:
            all_task_ids = [t if isinstance(t, str) else str(getattr(t, "task_id", t)) for t in task_index]

        sampled = stratified_sample(all_task_ids)
        logger.debug(
            "calibrate.orchestrated.entry",
            sut_id=sut_id,
            evalset_id=evalset_id,
            k=k,
            tasks=len(all_task_ids),
            sampled=len(sampled),
        )

        runs: list[dict[str, dict[str, float]]] = []
        for _ in range(max(2, k)):
            result = await run_batch_fn(sampled)
            runs.append(
                {
                    str(tid): {str(m): float(v) for m, v in (vals or {}).items()}
                    for tid, vals in (result or {}).items()
                }
            )

        # per metric: paired per-task values across runs
        metric_names: set[str] = set()
        for per_task in runs:
            for vals in per_task.values():
                metric_names.update(vals)

        metrics: dict[str, dict[str, float]] = {}
        for name in sorted(metric_names):
            sig_acc: list[float] = []
            n_pairs = 0
            for tid in sampled:
                series = [r[tid][name] for r in runs if tid in r and name in r[tid]]
                if len(series) >= 2:
                    sig_acc.append(_stdev(series))
                    n_pairs += 1
            if n_pairs == 0:
                continue
            sigma = sum(sig_acc) / n_pairs
            metrics[name] = {
                "sigma": sigma,
                "delta": self.factor * sigma,
                "n_pairs": float(n_pairs),
            }

        n_pairs_min = int(min((m["n_pairs"] for m in metrics.values()), default=0))
        low_confidence = n_pairs_min < len(sampled) / 2
        valid = (not low_confidence) and n_pairs_min >= 2
        evalset_hash = _hash_task_ids(sampled)

        self.last_sigma = {name: m["sigma"] for name, m in metrics.items()}
        self.last_n_pairs = n_pairs_min
        self.last_calibrated_at = datetime.now(UTC).isoformat()
        self.last_evalset_id = evalset_id
        self.last_evalset_hash = evalset_hash
        self.last_low_confidence = low_confidence
        self.last_valid = valid

        info = NoiseBandInfo(
            metrics=metrics,
            k=len(runs),
            evalset_hash=evalset_hash,
            low_confidence=low_confidence,
            valid=valid,
            n_pairs=n_pairs_min,
        )
        if evalset_id is not None:
            self._persist(sut_id, evalset_id, info)
        logger.debug(
            "calibrate.orchestrated.exit",
            metrics=len(metrics),
            n_pairs=n_pairs_min,
            low_confidence=low_confidence,
            valid=valid,
        )
        return info

    def _persist(self, sut_id: str, evalset_id: str, info: NoiseBandInfo) -> None:
        """Write one noise_bands row per metric (upsert on set-hash-metric)."""
        if self._session_factory is None:
            return
        from rsi.core.naming import new_id
        from rsi.db.tables import NoiseBand

        calibrated_at = self.last_calibrated_at or datetime.now(UTC).isoformat()
        with self._session_factory() as session:
            for metric, band in sorted(info.metrics.items()):
                row = (
                    session.query(NoiseBand)
                    .filter(
                        NoiseBand.evalset_id == evalset_id,
                        NoiseBand.evalset_hash == info.evalset_hash,
                        NoiseBand.metric == metric,
                    )
                    .first()
                )
                if row is None:
                    row = NoiseBand(
                        id=new_id("nb"),
                        evalset_id=evalset_id,
                        evalset_hash=info.evalset_hash,
                        metric=metric,
                    )
                    session.add(row)
                row.sigma = float(band["sigma"])
                row.delta = float(band["delta"])
                row.k = int(info.k)
                row.low_confidence = 1 if info.low_confidence else 0
                row.calibrated_at = calibrated_at
            session.commit()
        logger.debug(
            "calibrate.persisted", evalset_id=evalset_id, metrics=len(info.metrics)
        )

    # -- legacy pure-function mode -------------------------------------------

    def _calibrate_from_stats(
        self, batch_stats_runs: list[BatchStats], k: int = 3
    ) -> NoiseBandInfo:
        """Compute per-metric σ across same-config batch runs and the band
        width δ = factor × σ (single batch run per "pair")."""
        runs = list(batch_stats_runs)
        logger.debug("calibrate.entry", runs=len(runs), k=k, factor=self.factor)
        if len(runs) < k:
            logger.debug("calibrate.short_batch", runs=len(runs), expected=k)
        if not runs:
            self.last_sigma = {}
            self.last_n_pairs = 0
            return NoiseBandInfo(metrics={}, k=len(runs), n_pairs=0)

        metric_names: set[str] = set()
        for stats in runs:
            metric_names.update(stats.metrics)

        metrics: dict[str, dict[str, float]] = {}
        for name in sorted(metric_names):
            values = [
                float(stats.metrics[name])
                for stats in runs
                if name in stats.metrics
            ]
            sigma = _stdev(values)
            metrics[name] = {
                "sigma": sigma,
                "delta": self.factor * sigma,
                "n_pairs": float(len(values)),
            }

        n_pairs_min = int(min((m["n_pairs"] for m in metrics.values()), default=0))
        low_confidence = n_pairs_min < len(runs) / 2
        self.last_sigma = {name: m["sigma"] for name, m in metrics.items()}
        self.last_n_pairs = len(runs)
        self.last_calibrated_at = datetime.now(UTC).isoformat()
        self.last_low_confidence = low_confidence
        self.last_valid = (not low_confidence) and n_pairs_min >= 2
        info = NoiseBandInfo(
            metrics=metrics,
            k=len(runs),
            evalset_hash="",
            low_confidence=low_confidence,
            valid=self.last_valid,
            n_pairs=n_pairs_min,
        )
        logger.debug(
            "calibrate.exit", metrics=len(metrics), n_pairs=info.n_pairs
        )
        return info
