"""SignificanceTester — FR9.2 paired significance test.

Candidate and baseline are evaluated on the *same* eval tasks, so the
samples are paired: n < 30 → Wilcoxon signed-rank (non-parametric), n ≥ 30 →
paired t-test. scipy is used when importable; otherwise a simplified
normal-approximation fallback keeps behavior deterministic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from rsi.log import get_logger

__all__ = ["SignificanceResult", "SignificanceTester"]

logger = get_logger(__name__)

try:  # pragma: no cover - environment dependent
    from scipy import stats as _scipy_stats
except ImportError:  # pragma: no cover
    _scipy_stats = None


class SignificanceResult(BaseModel):
    """Outcome of one paired significance test."""

    model_config = ConfigDict(frozen=True)

    p_value: float
    significant: bool
    method: str
    n: int


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float:
    m = _mean(values)
    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5


def _normal_sf(z: float) -> float:
    """Survival function of the standard normal (Abramowitz-Stegun 7.1.26)."""
    from math import erf, sqrt

    return 0.5 * (1.0 - erf(z / sqrt(2.0)))


def _wilcoxon_fallback(diffs: list[float]) -> float:
    """Simplified Wilcoxon signed-rank: normal approximation on |rank| sums.

    Zero differences are dropped; ties get average ranks.
    """
    from math import sqrt

    nonzero = [d for d in diffs if d != 0.0]
    if not nonzero:
        return 1.0
    ordered = sorted(nonzero, key=abs)
    # average ranks for ties in |d|
    ranks: list[float] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and abs(ordered[j + 1]) == abs(ordered[i]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for _ in range(i, j + 1):
            ranks.append(avg)
        i = j + 1
    w_plus = sum(r for r, d in zip(ranks, ordered) if d > 0)
    n = len(nonzero)
    mu = n * (n + 1) / 4.0
    sigma2 = n * (n + 1) * (2 * n + 1) / 24.0
    if sigma2 <= 0:
        return 1.0
    z = (w_plus - mu) / sqrt(sigma2)
    return min(1.0, 2.0 * _normal_sf(abs(z)))


def _ttest_fallback(diffs: list[float]) -> float:
    """Paired t-test p-value via the normal approximation (n >= 30)."""
    n = len(diffs)
    sd = _stdev(diffs)
    if sd == 0.0:
        return 1.0
    t = (_mean(diffs) / (sd / n**0.5)) if sd else 0.0
    return min(1.0, 2.0 * _normal_sf(abs(t)))


class SignificanceTester:
    """FR9.2: paired Wilcoxon (n<30) / paired t-test (n>=30)."""

    def test(
        self,
        base_values: list[float],
        cand_values: list[float],
        alpha: float = 0.05,
    ) -> SignificanceResult:
        base = [float(v) for v in base_values]
        cand = [float(v) for v in cand_values]
        if len(base) != len(cand):
            raise ValueError(
                f"paired test requires equal lengths, got "
                f"{len(base)} vs {len(cand)}"
            )
        if not base:
            raise ValueError("paired test requires at least one sample")
        diffs = [c - b for b, c in zip(base, cand)]
        n = len(diffs)
        method = "wilcoxon" if n < 30 else "ttest"

        p_value: float
        if _scipy_stats is not None:
            nonzero = [d for d in diffs if d != 0.0]
            if method == "wilcoxon":
                if not nonzero:
                    p_value = 1.0
                else:
                    p_value = float(_scipy_stats.wilcoxon(nonzero).pvalue)
                    method = "wilcoxon"
            else:
                if _stdev(diffs) == 0.0:
                    p_value = 1.0
                else:
                    p_value = float(_scipy_stats.ttest_rel(cand, base).pvalue)
                    method = "paired_ttest"
        else:  # pragma: no cover - fallback path
            p_value = (
                _wilcoxon_fallback(diffs)
                if n < 30
                else _ttest_fallback(diffs)
            )

        significant = p_value < alpha
        logger.debug(
            "test.exit", method=method, n=n, p_value=p_value,
            significant=significant, alpha=alpha,
        )
        return SignificanceResult(
            p_value=p_value,
            significant=significant,
            method=method,
            n=n,
        )
