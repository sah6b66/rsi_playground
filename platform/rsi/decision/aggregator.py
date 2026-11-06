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
    def aggregate(
        self,
        verdicts: list[Verdict],
        task_ids: list[str] | None = None,
    ) -> BatchStats:
        """Aggregate verdicts into batch stats.

        ``task_ids`` (same length/order as *verdicts*, or per-verdict
        ``task_id`` attributes) additionally produces ``task_scores``
        entries — per-task ``{task_id, score, task_complete, missing}``
        consumed by the significance tester and hit-rate recalc (D-9).
        """
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
            task_scores=self._task_scores(verdicts, task_ids),
        )
        logger.debug("aggregate.exit", n=stats.n, missing=stats.missing)
        return stats

    @staticmethod
    def _task_scores(
        verdicts: list[Verdict], task_ids: list[str] | None
    ) -> list[dict]:
        """Per-task score rows (D-9): {task_id, score, task_complete, missing}.

        ``score`` is the primary per-task score (root_cause_accuracy,
        falling back to task_complete) used for pairing / hit-rate calc.
        """
        rows: list[dict] = []
        for i, v in enumerate(verdicts):
            tid: str | None = None
            if task_ids is not None and i < len(task_ids):
                tid = str(task_ids[i])
            else:
                tid = getattr(v, "task_id", None)
            if tid is None:
                continue
            scores = v.scores
            score = float(getattr(scores, "root_cause_accuracy", 0.0) or 0.0)
            task_complete = float(bool(getattr(scores, "task_complete", False)))
            if score == 0.0 and task_complete:
                score = task_complete
            rows.append(
                {
                    "task_id": tid,
                    "score": score,
                    "task_complete": task_complete,
                    "missing": bool(v.missing),
                }
            )
        return rows
