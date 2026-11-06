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
    """Store decisions in memory (API consumers) and, when wired, persist
    each decision to the ``experiments`` table + audit log (DD-10 §4,
    one logical transaction per record)."""

    # Decision.action -> experiments.decision (check constraint allows only
    # promoted/reverted/pending/advise_manual; REJECT collapses to
    # advise_manual with the original action kept in reason/evidence).
    _DB_DECISION: dict[str, str] = {
        "PROMOTE": "promoted",
        "REVERT": "reverted",
        "ADVISE_MANUAL": "advise_manual",
        "REJECT": "advise_manual",
    }

    def __init__(self, session_factory=None, audit_log=None) -> None:
        self._records: dict[str, list[DecisionRecord]] = {}
        self._session_factory = session_factory
        self._audit_log = audit_log

    def record(
        self,
        experiment_id: str,
        decision: Decision,
        baseline_batch_id: str,
        candidate_batch_id: str,
        *,
        sut_id: str = "unknown",
        baseline_version_id: str = "unknown",
        candidate_version_id: str = "unknown",
        hypothesis_id: str | None = None,
        scores: dict[str, Any] | None = None,
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
        self._persist(
            record,
            sut_id=sut_id,
            baseline_version_id=baseline_version_id,
            candidate_version_id=candidate_version_id,
            hypothesis_id=hypothesis_id,
            scores=scores,
        )
        logger.debug(
            "record",
            experiment_id=experiment_id,
            action=decision.action,
            rule_hit=decision.rule_hit,
        )

    def _persist(
        self,
        record: DecisionRecord,
        *,
        sut_id: str,
        baseline_version_id: str,
        candidate_version_id: str,
        hypothesis_id: str | None,
        scores: dict[str, Any] | None,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            self._persist_now(
                record,
                sut_id=sut_id,
                baseline_version_id=baseline_version_id,
                candidate_version_id=candidate_version_id,
                hypothesis_id=hypothesis_id,
                scores=scores,
            )
        except Exception as exc:  # noqa: BLE001 - R2-H1: a DB/audit outage
            # must not crash the DECIDING phase; the in-memory record (kept
            # above) is still served to API consumers, same containment
            # policy as Accountant/GitStore.
            logger.warning(
                "decision.persist_failed",
                experiment_id=record.experiment_id,
                error=str(exc),
            )

    def _persist_now(
        self,
        record: DecisionRecord,
        *,
        sut_id: str,
        baseline_version_id: str,
        candidate_version_id: str,
        hypothesis_id: str | None,
        scores: dict[str, Any] | None,
    ) -> None:
        import json

        from rsi.db.tables import Experiment

        decision = record.decision
        db_decision = self._DB_DECISION.get(decision.action, "pending")
        reason = decision.reason
        if decision.action == "REJECT":
            reason = f"[REJECT] {reason}"
        evidence_json = json.dumps(
            {
                "evidence": decision.evidence,
                "rule_hit": decision.rule_hit,
                "action": decision.action,
                "baseline_batch_id": record.baseline_batch_id,
                "candidate_batch_id": record.candidate_batch_id,
                "scores": scores or {},
                "recorded_at": record.recorded_at,
            },
            ensure_ascii=False,
            default=str,
        )
        with self._session_factory() as session:
            row = session.get(Experiment, record.experiment_id)
            if row is None:
                row = Experiment(
                    id=record.experiment_id,
                    sut_id=sut_id,
                    hypothesis_id=hypothesis_id,
                    baseline_version_id=baseline_version_id,
                    candidate_version_id=candidate_version_id,
                    created_at=record.recorded_at,
                )
                session.add(row)
            row.decision = db_decision
            row.decision_reason = reason
            row.evidence_json = evidence_json
            row.hypothesis_id = hypothesis_id or row.hypothesis_id
            row.baseline_batch_id = record.baseline_batch_id
            row.candidate_batch_id = record.candidate_batch_id
            session.commit()
        if self._audit_log is not None:
            self._audit_log.record(
                actor="system",
                action="decision.record",
                object_type="experiment",
                object_id=record.experiment_id,
                detail={
                    "decision": decision.action,
                    "rule_hit": decision.rule_hit,
                    "db_decision": db_decision,
                    "baseline_batch_id": record.baseline_batch_id,
                    "candidate_batch_id": record.candidate_batch_id,
                },
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
