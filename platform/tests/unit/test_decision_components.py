"""TU: NoiseBandCalibrator + SignificanceTester + DecisionRecorder (DD-10)."""

from __future__ import annotations

import pytest

from rsi.core.models import BatchStats, Decision
from rsi.decision.noise import NoiseBandCalibrator
from rsi.decision.recorder import DecisionRecorder
from rsi.decision.significance import SignificanceTester


def _stats(metrics: dict[str, float]) -> BatchStats:
    return BatchStats(
        metrics=metrics,
        n=4,
        missing=0,
        tokens=100,
        tool_calls=5,
        wall_time_s=10.0,
        tokens_estimated=False,
    )


class TestNoiseBandCalibrator:
    def test_sigma_and_delta_from_known_runs(self) -> None:
        # values per run for root_cause_accuracy: 0.80, 0.90, 1.00
        # mean 0.90, sigma = sqrt(((0.1)^2*2 + 0)/3) = 0.081649658...
        cal = NoiseBandCalibrator()
        info = cal.calibrate(
            [
                _stats({"root_cause_accuracy": 0.80, "task_complete": 1.0}),
                _stats({"root_cause_accuracy": 0.90, "task_complete": 1.0}),
                _stats({"root_cause_accuracy": 1.00, "task_complete": 1.0}),
            ]
        )
        assert info.n_pairs == 3
        sigma = (2 * 0.01 / 3) ** 0.5
        assert cal.last_sigma["root_cause_accuracy"] == pytest.approx(sigma)
        # delta = 1.96 * sigma (default factor); D-8 nested metric shape
        assert info.metrics["root_cause_accuracy"]["sigma"] == pytest.approx(sigma)
        assert info.metrics["root_cause_accuracy"]["delta"] == pytest.approx(1.96 * sigma)
        assert info.metrics["root_cause_accuracy"]["n_pairs"] == 3.0
        # zero-variance metric -> zero band
        assert info.metrics["task_complete"]["delta"] == pytest.approx(0.0)

    def test_constant_runs_give_zero_band(self) -> None:
        cal = NoiseBandCalibrator()
        info = cal.calibrate([_stats({"task_complete": 1.0})] * 3)
        assert info.metrics["task_complete"]["delta"] == 0.0
        assert info.n_pairs == 3
        assert info.valid is True

    def test_empty_runs(self) -> None:
        info = NoiseBandCalibrator().calibrate([])
        assert info.metrics == {}
        assert info.n_pairs == 0

    def test_band_feeds_selector_noise_delta(self) -> None:
        # the calibrated band is what DecisionCtx.noise_delta consumes
        info = NoiseBandCalibrator().calibrate(
            [_stats({"root_cause_accuracy": v}) for v in (0.7, 0.8, 0.9)]
        )
        assert info.metrics["root_cause_accuracy"]["delta"] > 0.0


class TestSignificanceTester:
    def test_small_n_uses_wilcoxon_significant(self) -> None:
        base = [0.5, 0.6, 0.4, 0.7, 0.5, 0.6]
        cand = [0.9, 0.9, 0.8, 0.9, 0.9, 0.9]  # all strictly better
        res = SignificanceTester().test(base, cand, alpha=0.05)
        assert "wilcoxon" in res.method
        assert res.n == 6
        assert 0.0 <= res.p_value <= 1.0
        assert res.p_value < 0.05
        assert res.significant is True

    def test_identical_values_not_significant(self) -> None:
        res = SignificanceTester().test([0.5] * 8, [0.5] * 8)
        assert res.p_value == pytest.approx(1.0)
        assert res.significant is False

    def test_large_n_uses_ttest(self) -> None:
        base = [0.5 + (i % 3) * 0.01 for i in range(40)]
        cand = [0.9 + ((i + 1) % 3) * 0.01 for i in range(40)]
        res = SignificanceTester().test(base, cand, alpha=0.05)
        assert "ttest" in res.method
        assert res.significant is True

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            SignificanceTester().test([1.0, 2.0], [1.0])


class TestDecisionRecorder:
    def _decision(self, action: str = "PROMOTE") -> Decision:
        return Decision(
            action=action, rule_hit=5, reason="r", evidence={"weighted_score": 0.2}
        )

    def test_record_then_retrieve(self) -> None:
        rec = DecisionRecorder()
        rec.record("exp-1", self._decision(), "batch-base", "batch-cand")
        records = rec.get("exp-1")
        assert len(records) == 1
        assert records[0].experiment_id == "exp-1"
        assert records[0].baseline_batch_id == "batch-base"
        assert records[0].candidate_batch_id == "batch-cand"
        assert records[0].decision.action == "PROMOTE"
        assert records[0].recorded_at

    def test_latest_and_missing_experiment(self) -> None:
        rec = DecisionRecorder()
        assert rec.get("nope") == []
        assert rec.latest("nope") is None
        rec.record("exp-2", self._decision("REVERT"), "b1", "b2")
        rec.record("exp-2", self._decision("PROMOTE"), "b1", "b3")
        assert len(rec.get("exp-2")) == 2
        assert rec.latest("exp-2").decision.action == "PROMOTE"
        assert rec.latest("exp-2").candidate_batch_id == "b3"

    def test_evidence_chain(self) -> None:
        rec = DecisionRecorder()
        rec.record("exp-3", self._decision(), "base-b", "cand-b")
        chain = rec.evidence_chain("exp-3")
        assert chain["baseline_batch_id"] == "base-b"
        assert chain["candidate_batch_id"] == "cand-b"
        assert chain["rule_hit"] == 5
        assert "weighted_score" in chain["evidence"]
