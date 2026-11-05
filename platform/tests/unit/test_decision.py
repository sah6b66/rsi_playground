"""TU for StatsAggregator + RegularizedSelector — DD-10 §4/§5.1/§5.2."""

from __future__ import annotations

import pytest

from rsi.core.models import (
    BatchStats,
    Decision,
    Efficiency,
    Goal,
    GuardPolicy,
    PathQuality,
    SafetyVerdict,
    Scores,
    Verdict,
)
from rsi.decision.aggregator import StatsAggregator
from rsi.decision.selector import DecisionCtx, RegularizedSelector


def _verdict(
    *,
    accuracy: float = 1.0,
    complete: bool = True,
    steps: int = 3,
    missed: int = 0,
    fp: int = 0,
    missing: bool = False,
    tokens: int = 100,
    calls: int = 5,
    wall: float = 10.0,
) -> Verdict:
    return Verdict(
        scores=Scores(
            root_cause_accuracy=accuracy,
            task_complete=complete,
            diagnostic_steps=steps,
            missed_check=missed,
            false_positive=fp,
        ),
        path_quality=PathQuality(verdict="pass", reasoning="r"),
        efficiency=Efficiency(tokens=tokens, tool_calls=calls, wall_time_s=wall),
        safety=SafetyVerdict(),
        missing=missing,
    )


def _stats(**overrides) -> BatchStats:
    fields = dict(
        metrics={"root_cause_accuracy": 0.8, "task_complete": 1.0},
        n=4,
        missing=0,
        tokens=400,
        tool_calls=20,
        wall_time_s=40.0,
        tokens_estimated=False,
    )
    fields.update(overrides)
    return BatchStats(**fields)


def _ctx(**overrides) -> DecisionCtx:
    fields = dict(
        base_stats=_stats(),
        cand_stats=_stats(metrics={"root_cause_accuracy": 1.0, "task_complete": 1.0}),
        noise_delta=0.05,
        goals=[Goal(metric="root_cause_accuracy", direction="max", weight=1.0)],
        guards=GuardPolicy(),
        cost={"cost_penalty": 0.1},
        hack_or_fatal=False,
    )
    fields.update(overrides)
    return DecisionCtx(**fields)


# ---------------------------------------------------------------------------
# StatsAggregator
# ---------------------------------------------------------------------------


def test_means_with_missing_kept_in_denominator() -> None:
    verdicts = [
        _verdict(accuracy=1.0),
        _verdict(accuracy=1.0),
        _verdict(accuracy=0.0, missing=True),
    ]
    stats = StatsAggregator().aggregate(verdicts)
    assert stats.n == 3
    assert stats.missing == 1
    assert stats.metrics["root_cause_accuracy"] == pytest.approx(2 / 3)
    assert stats.metrics["task_complete"] == pytest.approx(2 / 3)
    # diagnostic_steps of the missing run counts as 0: (3 + 3 + 0) / 3
    assert stats.metrics["diagnostic_steps"] == pytest.approx(2.0)
    assert stats.metrics["missed_check"] == 0.0
    assert stats.metrics["false_positive"] == 0.0
    assert stats.tokens == 300
    assert stats.tool_calls == 15
    assert stats.wall_time_s == pytest.approx(30.0)
    assert stats.tokens_estimated is False


def test_missing_scores_are_zero_not_skipped() -> None:
    verdicts = [_verdict(accuracy=1.0), _verdict(accuracy=1.0, missing=True)]
    stats = StatsAggregator().aggregate(verdicts)
    assert stats.metrics["root_cause_accuracy"] == pytest.approx(0.5)


def test_sums_efficiency() -> None:
    verdicts = [
        _verdict(tokens=10, calls=1, wall=1.0),
        _verdict(tokens=20, calls=2, wall=2.5),
    ]
    stats = StatsAggregator().aggregate(verdicts)
    assert stats.tokens == 30
    assert stats.tool_calls == 3
    assert stats.wall_time_s == pytest.approx(3.5)


def test_empty_batch() -> None:
    stats = StatsAggregator().aggregate([])
    assert stats.n == 0
    assert stats.metrics == {}
    assert stats.tokens == 0


# ---------------------------------------------------------------------------
# RegularizedSelector — rules 0..6 in order
# ---------------------------------------------------------------------------


def test_rule0_high_missing_rate_advise_manual() -> None:
    ctx = _ctx(cand_stats=_stats(n=10, missing=4))  # 40% > 30%
    d = RegularizedSelector().decide(ctx)
    assert d.action == "ADVISE_MANUAL"
    assert d.rule_hit == 0


def test_rule0_boundary_30pct_not_fired() -> None:
    ctx = _ctx(cand_stats=_stats(n=10, missing=3))  # exactly 30% -> not > 0.30
    d = RegularizedSelector().decide(ctx)
    assert d.rule_hit != 0


def test_rule1_hack_or_fatal_reject() -> None:
    ctx = _ctx(hack_or_fatal=True)
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REJECT"
    assert d.rule_hit == 1


def test_rule2_missed_check_worsened_reject() -> None:
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8, "missed_check": 0.0}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.9, "missed_check": 1.0}),
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REJECT"
    assert d.rule_hit == 2


def test_rule2_safety_or_high_risk_worsened_reject() -> None:
    for key in ("safety_violations", "high_risk_actions"):
        ctx = _ctx(
            base_stats=_stats(metrics={"root_cause_accuracy": 0.8, key: 0.0}),
            cand_stats=_stats(metrics={"root_cause_accuracy": 0.95, key: 1.0}),
        )
        d = RegularizedSelector().decide(ctx)
        assert d.action == "REJECT"
        assert d.rule_hit == 2


def test_rule3_within_noise_revert() -> None:
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.80}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.83}),  # gain 0.03 < delta 0.05
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REVERT"
    assert d.rule_hit == 3


def test_rule3_uncalibrated_zero_delta_any_nonpositive_reverts() -> None:
    ctx = _ctx(
        noise_delta=None,
        base_stats=_stats(metrics={"root_cause_accuracy": 0.9}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.9}),  # delta 0 <= 0
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REVERT"
    assert d.rule_hit == 3


def test_rule4_cost_penalty_reject() -> None:
    # gain above noise (1.0 - 0.8 - 0.05 = 0.15) but below cost penalty 0.2
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 1.0}),
        cost={"cost_penalty": 0.2},
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REJECT"
    assert d.rule_hit == 4


def test_rule5_significant_promote() -> None:
    ctx = _ctx()  # gain 0.1, delta 0.05, penalty 0.1 -> net 0.0 not < penalty
    d = RegularizedSelector().decide(ctx)
    assert d.action == "PROMOTE"
    assert d.rule_hit == 5


def test_rule6_uncalibrated_advise_manual() -> None:
    ctx = _ctx(
        noise_delta=None,
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 1.0}),
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "ADVISE_MANUAL"
    assert d.rule_hit == 6


def test_min_direction_metric_inverts_delta() -> None:
    # wall_time_s is a "min" goal and is not a guard metric, so rule 2 stays quiet
    ctx = _ctx(
        goals=[Goal(metric="wall_time_s", direction="min", weight=1.0)],
        base_stats=_stats(metrics={"wall_time_s": 2.0}),
        cand_stats=_stats(metrics={"wall_time_s": 0.0}),  # improvement for min metric
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action in ("PROMOTE", "ADVISE_MANUAL")
    assert d.rule_hit in (5, 6)
    # worsening a min metric should fall to rule 3 (S negative)
    ctx_bad = _ctx(
        goals=[Goal(metric="wall_time_s", direction="min", weight=1.0)],
        base_stats=_stats(metrics={"wall_time_s": 0.0}),
        cand_stats=_stats(metrics={"wall_time_s": 2.0}),
    )
    d_bad = RegularizedSelector().decide(ctx_bad)
    assert d_bad.action == "REVERT"
    assert d_bad.rule_hit == 3


def test_decision_evidence_carries_metrics() -> None:
    d = RegularizedSelector().decide(_ctx())
    assert isinstance(d, Decision)
    assert d.reason
    assert "weighted_score" in d.evidence
