"""StatsAggregator — per-batch metric aggregation (DD-10 §4/§5).

Missing runs count as 0 and stay in the denominator (缺试计 0 保留分母), so a
flaky environment does not inflate means. Defense-in-depth: even if a Verdict
arrives with both ``missing=True`` and nonzero scores, the 0 is enforced here.
"""

from __future__ import annotations

from rsi.core.models import BatchStats, Verdict
from rsi.log import get_logger

__all__ = ["StatsAggregator"]

logger = get_logger(__name__)

_MEAN_METRICS: tuple[tuple[str, str], ...] = (
    # (BatchStats.metrics key, Verdict source accessor)
    ("root_cause_accuracy", "scores.root_cause_accuracy"),
    ("task_complete", "scores.task_complete"),
    ("diagnostic_steps", "scores.diagnostic_steps"),
    ("missed_check", "scores.missed_check"),
    ("false_positive", "scores.false_positive"),
)


class StatsAggregator:
    def aggregate(self, verdicts: list[Verdict]) -> BatchStats:
        logger.debug("aggregate.entry", verdicts=len(verdicts))
        n = len(verdicts)
        metrics: dict[str, float] = {}
        if n:
            for name, path in _MEAN_METRICS:
                values: list[float] = []
                for v in verdicts:
                    if v.missing:
                        values.append(0.0)  # missing counts as 0, keeps denominator
                        continue
                    obj: object = v
                    for attr in path.split("."):
                        obj = getattr(obj, attr)
                    values.append(float(obj))
                metrics[name] = sum(values) / n

        stats = BatchStats(
            metrics=metrics,
            n=n,
            missing=sum(1 for v in verdicts if v.missing),
            tokens=sum(v.efficiency.tokens for v in verdicts),
            tool_calls=sum(v.efficiency.tool_calls for v in verdicts),
            wall_time_s=round(sum(v.efficiency.wall_time_s for v in verdicts), 3),
            tokens_estimated=any(v.efficiency.estimated for v in verdicts),
        )
        logger.debug("aggregate.exit", n=stats.n, missing=stats.missing)
        return stats
