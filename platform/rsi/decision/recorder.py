"""DecisionRecorder — FR9.5 decision evidence chain.

Every iteration decision is stored with its full evidence chain: the
experiment it belongs to, the baseline/candidate batch ids the decision
compared, and the frozen ``Decision`` (action / rule_hit / reason /
evidence). P0 keeps records in memory keyed by experiment id; the API layer
serializes them into the audit log / DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from rsi.core.models import Decision
from rsi.log import get_logger

__all__ = ["DecisionRecord", "DecisionRecorder"]

logger = get_logger(__name__)


class DecisionRecord(BaseModel):
    """One stored decision with its evidence chain (FR9.5)."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    decision: Decision
    baseline_batch_id: str
    candidate_batch_id: str
    recorded_at: str


class DecisionRecorder:
    def __init__(self) -> None:
        self._records: dict[str, list[DecisionRecord]] = {}

    def record(
        self,
        experiment_id: str,
        decision: Decision,
        baseline_batch_id: str,
        candidate_batch_id: str,
    ) -> None:
        """Store *decision* with its evidence chain under *experiment_id*."""
        record = DecisionRecord(
            experiment_id=experiment_id,
            decision=decision,
            baseline_batch_id=baseline_batch_id,
            candidate_batch_id=candidate_batch_id,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        self._records.setdefault(experiment_id, []).append(record)
        logger.debug(
            "record",
            experiment_id=experiment_id,
            action=decision.action,
            rule_hit=decision.rule_hit,
        )

    def get(self, experiment_id: str) -> list[DecisionRecord]:
        """All decision records for one experiment, in record order."""
        return list(self._records.get(experiment_id, []))

    def latest(self, experiment_id: str) -> DecisionRecord | None:
        records = self._records.get(experiment_id, [])
        return records[-1] if records else None

    def all_records(self) -> list[DecisionRecord]:
        """Flat view of every recorded decision (audit export)."""
        out: list[DecisionRecord] = []
        for records in self._records.values():
            out.extend(records)
        return out

    def evidence_chain(self, experiment_id: str) -> dict[str, Any]:
        """The evidence chain of the latest decision for *experiment_id*."""
        rec = self.latest(experiment_id)
        if rec is None:
            return {}
        return {
            "experiment_id": rec.experiment_id,
            "baseline_batch_id": rec.baseline_batch_id,
            "candidate_batch_id": rec.candidate_batch_id,
            "action": rec.decision.action,
            "rule_hit": rec.decision.rule_hit,
            "reason": rec.decision.reason,
            "evidence": rec.decision.evidence,
            "recorded_at": rec.recorded_at,
        }
