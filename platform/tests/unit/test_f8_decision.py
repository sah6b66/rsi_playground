"""F8 tests: D-8 orchestrated noise calibration + D-9 recorder/aggregator."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from sqlmodel import Session as SqlSession

from rsi.core.models import Decision
from rsi.decision.noise import NoiseBandCalibrator, stratified_sample
from rsi.decision.recorder import DecisionRecorder


def _db_factory(tmp_path):
    import rsi.db.tables  # noqa: F401 — register models

    engine = create_engine(f"sqlite:///{tmp_path}/noise.db")
    SQLModel.metadata.create_all(engine)
    return sessionmaker(bind=engine, class_=SqlSession)


def _task_index(n: int = 6) -> dict[str, dict]:
    # two domains (fault/, config/) for stratified sampling
    out: dict[str, dict] = {f"fault/t-{i:02d}": {} for i in range(n // 2)}
    out.update({f"config/t-{i:02d}": {} for i in range(n // 2)})
    return out


class TestOrchestratedCalibration:
    def test_sigma_delta_and_db_row(self, tmp_path):
        factory = _db_factory(tmp_path)
        cal = NoiseBandCalibrator(session_factory=factory)

        state = {"calls": 0}

        def run_batch_fn(task_ids):
            state["calls"] += 1
            sign = 1 if state["calls"] % 2 else -1

            async def _run(task_ids):
                return {
                    tid: {"root_cause_accuracy": 0.8 + sign * (0.1 if i % 2 else 0.0)}
                    for i, tid in enumerate(task_ids)
                }

            return _run(task_ids)

        info = asyncio.run(
            cal.calibrate(
                "sut-1",
                "es-1",
                k=2,
                run_batch_fn=run_batch_fn,
                task_index=_task_index(),
            )
        )
        # odd-index tasks differ by 0.2 between passes -> per-task stdev 0.1;
        # averaged with the even tasks (stdev 0) -> sigma 0.05
        band = info.metrics["root_cause_accuracy"]
        assert band["sigma"] == pytest.approx(0.05)
        assert band["delta"] == pytest.approx(1.96 * band["sigma"])
        assert band["n_pairs"] == 6.0
        assert info.n_pairs == 6
        assert info.low_confidence is False
        assert info.valid is True
        assert info.k == 2
        assert info.evalset_hash

        from rsi.db.tables import NoiseBand

        with factory() as session:
            rows = session.query(NoiseBand).all()
            assert len(rows) == 1
            assert rows[0].metric == "root_cause_accuracy"
            assert rows[0].evalset_id == "es-1"
            assert rows[0].sigma == pytest.approx(band["sigma"])
            assert rows[0].low_confidence == 0

    def test_low_confidence_when_few_valid_tasks(self):
        cal = NoiseBandCalibrator()

        def run_batch_fn(task_ids):
            async def _run(task_ids):
                # only one task returns scores -> 1 < 6/2 -> low confidence
                return {task_ids[0]: {"rca": 0.8}}

            return _run(task_ids)

        info = asyncio.run(
            cal.calibrate(
                "sut-1",
                "es-2",
                k=2,
                run_batch_fn=run_batch_fn,
                task_index=_task_index(),
            )
        )
        assert info.low_confidence is True
        assert info.valid is False
        assert info.n_pairs == 1

    def test_stratified_sampling_covers_domains(self):
        tasks = [f"fault/{i}" for i in range(20)] + [f"config/{i}" for i in range(5)]
        picked = stratified_sample(tasks)
        assert len(picked) == 10
        domains = {t.split("/")[0] for t in picked}
        assert domains == {"fault", "config"}

    def test_orchestrated_requires_run_batch_fn(self):
        cal = NoiseBandCalibrator()
        with pytest.raises(ValueError):
            asyncio.run(cal.calibrate("sut-1", "es-1", task_index={"a/1": {}}))


class TestRecorderPersistence:
    def _decision(self, action: str = "PROMOTE") -> Decision:
        return Decision(
            action=action, rule_hit=5, reason="ok", evidence={"weighted_score": 0.2}
        )

    def test_record_writes_experiment_row_and_audit(self, tmp_path):
        from rsi.security.audit import AuditLog

        factory = _db_factory(tmp_path)
        audit = AuditLog()
        rec = DecisionRecorder(session_factory=factory, audit_log=audit)
        rec.record(
            "exp-001",
            self._decision(),
            "batch-base",
            "batch-cand",
            sut_id="sut-1",
            baseline_version_id="v1",
            candidate_version_id="v2",
            scores={"task_complete": 0.9},
        )
        from rsi.db.tables import Experiment

        with factory() as session:
            row = session.get(Experiment, "exp-001")
            assert row is not None
            assert row.decision == "promoted"
            assert row.decision_reason == "ok"
            assert row.baseline_batch_id == "batch-base"
            assert row.candidate_batch_id == "batch-cand"
            assert row.sut_id == "sut-1"
            assert "weighted_score" in row.evidence_json

        entries = audit.entries
        assert any(getattr(e, "action", None) == "decision.record" for e in entries)

    def test_reject_maps_to_advise_manual_row(self, tmp_path):
        factory = _db_factory(tmp_path)
        rec = DecisionRecorder(session_factory=factory)
        rec.record(
            "exp-002",
            self._decision("REJECT"),
            "b1",
            "b2",
        )
        from rsi.db.tables import Experiment

        with factory() as session:
            row = session.get(Experiment, "exp-002")
            assert row.decision == "advise_manual"
            assert row.decision_reason.startswith("[REJECT]")

    def test_second_record_updates_same_row(self, tmp_path):
        factory = _db_factory(tmp_path)
        rec = DecisionRecorder(session_factory=factory)
        rec.record("exp-003", self._decision("PROMOTE"), "b1", "b2")
        rec.record("exp-003", self._decision("REVERT"), "b1", "b3")
        from rsi.db.tables import Experiment

        with factory() as session:
            assert session.query(Experiment).count() == 1
            assert session.get(Experiment, "exp-003").decision == "reverted"


class TestAggregatorTaskScores:
    def _verdict(self, rca: float, missing: bool = False):
        from rsi.core.models import (
            Efficiency,
            PathQuality,
            SafetyVerdict,
            Scores,
            Verdict,
        )

        return Verdict(
            scores=Scores(
                root_cause_accuracy=rca,
                task_complete=rca >= 0.8,
                diagnostic_steps=3,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", reasoning="r"),
            efficiency=Efficiency(tokens=10, tool_calls=1, wall_time_s=1.0),
            safety=SafetyVerdict(),
            missing=missing,
        )

    def test_task_scores_produced_with_task_ids(self):
        from rsi.decision.aggregator import StatsAggregator

        stats = StatsAggregator().aggregate(
            [self._verdict(0.9), self._verdict(0.5), self._verdict(0.0, missing=True)],
            task_ids=["fault/a", "fault/b", "config/c"],
        )
        assert len(stats.task_scores) == 3
        first = stats.task_scores[0]
        assert first["task_id"] == "fault/a"
        assert first["score"] == pytest.approx(0.9)
        assert first["task_complete"] == 1.0
        assert stats.task_scores[2]["missing"] is True
        assert stats.task_scores[2]["score"] == 0.0

    def test_task_scores_empty_without_ids(self):
        from rsi.decision.aggregator import StatsAggregator

        stats = StatsAggregator().aggregate([self._verdict(0.9)])
        assert stats.task_scores == []

    def test_hit_rate_from_production_task_scores(self):
        from rsi.decision.aggregator import StatsAggregator
        from rsi.orchestrator.iteration import IterationEngine

        base = StatsAggregator().aggregate(
            [self._verdict(0.4), self._verdict(0.4), self._verdict(0.4)],
            task_ids=["t1", "t2", "t3"],
        )
        cand = StatsAggregator().aggregate(
            [self._verdict(0.9), self._verdict(0.4), self._verdict(0.4)],
            task_ids=["t1", "t2", "t3"],
        )
        hyp = {"predicted_affected_tasks": ["t1", "t2", "t3"]}
        IterationEngine._calc_hit_rate(hyp, base, cand)
        assert hyp["hit_rate"] == pytest.approx(1 / 3)
        assert hyp["improved_tasks"] == ["t1"]
