"""NoiseBandCalibrator — ADR-17 explicit calibration batch (DD-10 §5.2).

Same-config batches are re-run k times; per-metric σ is the standard
deviation of the metric values across those runs and the noise band width
δ = factor × σ (factor default 1.96, the 95% two-sided normal quantile).
The resulting band feeds ``DecisionCtx.noise_delta`` so rule 3 / rule 5 of
the RegularizedSelector operate on a calibrated band instead of 0.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rsi.core.models import BatchStats, NoiseBandInfo
from rsi.log import get_logger

__all__ = ["NoiseBandCalibrator"]

logger = get_logger(__name__)

_FACTOR_95 = 1.96


def _stdev(values: list[float]) -> float:
    """Population standard deviation of *values* (len >= 1)."""
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / n
    return var**0.5


class NoiseBandCalibrator:
    """Calibrate the per-metric noise band from explicit repeat batches."""

    def __init__(self, factor: float = _FACTOR_95) -> None:
        self.factor = factor
        # Last calibration's raw per-metric sigma (evidence, not in the band model).
        self.last_sigma: dict[str, float] = {}
        # API (§9.17) needs the last calibration metadata as well.
        self.last_n_pairs: int = 0
        self.last_calibrated_at: str | None = None

    def calibrate(
        self, batch_stats_runs: list[BatchStats], k: int = 3
    ) -> NoiseBandInfo:
        """Compute per-metric σ across *k* same-config batch runs and the
        band width δ = factor × σ.

        Metrics missing from a run count as absent observations only; the
        union of metric keys across runs is calibrated. ``k`` is the expected
        number of runs (ADR-17 calibration batch size); fewer runs than k is
        accepted but logged — a single run yields an all-zero band.
        """
        runs = list(batch_stats_runs)
        logger.debug(
            "calibrate.entry", runs=len(runs), k=k, factor=self.factor
        )
        if len(runs) < k:
            logger.debug(
                "calibrate.short_batch", runs=len(runs), expected=k
            )
        if not runs:
            self.last_sigma = {}
            self.last_n_pairs = 0
            return NoiseBandInfo(metrics={}, n_pairs=0)

        metric_names: set[str] = set()
        for stats in runs:
            metric_names.update(stats.metrics)

        sigma: dict[str, float] = {}
        band: dict[str, float] = {}
        for name in sorted(metric_names):
            values = [
                float(stats.metrics[name])
                for stats in runs
                if name in stats.metrics
            ]
            sigma[name] = _stdev(values)
            band[name] = self.factor * sigma[name]

        self.last_sigma = sigma
        self.last_n_pairs = len(runs)
        self.last_calibrated_at = datetime.now(UTC).isoformat()
        info = NoiseBandInfo(metrics=band, n_pairs=len(runs))
        logger.debug(
            "calibrate.exit", metrics=len(band), n_pairs=info.n_pairs
        )
        return info
