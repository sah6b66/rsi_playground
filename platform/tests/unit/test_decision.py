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
    # D-7 relative cost: gain above noise (1.0 - 0.8 - 0.05 = 0.15) but the
    # candidate doubles tokens -> relative cost 1.0 -> threshold 0.2 -> reject
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8}, tokens=400),
        cand_stats=_stats(metrics={"root_cause_accuracy": 1.0}, tokens=800),
        cost={"cost_penalty": 0.2},
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REJECT"
    assert d.rule_hit == 4
    assert d.evidence["relative_cost"] == pytest.approx(1.0)


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


# ---------------------------------------------------------------------------
# D-7: weighted composite score / relative cost / significance / guard switches
# ---------------------------------------------------------------------------


def test_composite_score_mixed_directions_with_metric_noise() -> None:
    # two goals: rca (max) improves 0.1 with sigma 0.05 -> +2.0;
    # wall_time_s (min) worsens 0.2 with sigma 0.1 -> -2.0
    base = _stats(metrics={"root_cause_accuracy": 0.8, "wall_time_s": 1.0})
    cand = _stats(metrics={"root_cause_accuracy": 0.9, "wall_time_s": 1.2})
    ctx = _ctx(
        base_stats=base,
        cand_stats=cand,
        noise_delta=0.5,
        goals=[
            Goal(metric="root_cause_accuracy", direction="max", weight=1.0),
            Goal(metric="wall_time_s", direction="min", weight=1.0),
        ],
        metric_noise={"root_cause_accuracy": 0.05, "wall_time_s": 0.1},
        cost={"cost_penalty": 0.0},
    )
    d = RegularizedSelector().decide(ctx)
    # S = 1*(0.1/0.05) + 1*(-0.2/0.1) = 0.0 <= delta 0.5 -> REVERT (rule 3)
    assert d.action == "REVERT"
    assert d.rule_hit == 3
    assert d.evidence["weighted_score"] == pytest.approx(0.0)


def test_composite_score_noise_normalization_changes_outcome() -> None:
    # same deltas, larger sigma -> smaller normalized score falls inside band
    base = _stats(metrics={"root_cause_accuracy": 0.8})
    cand = _stats(metrics={"root_cause_accuracy": 0.9})
    ctx = _ctx(
        base_stats=base,
        cand_stats=cand,
        noise_delta=0.5,  # band applies to the normalized score
        metric_noise={"root_cause_accuracy": 0.1},  # 0.1/0.1 = 1.0 > 0.5
        cost={"cost_penalty": 0.0},
    )
    assert RegularizedSelector().decide(ctx).action == "PROMOTE"

    ctx_tight = _ctx(
        base_stats=base,
        cand_stats=cand,
        noise_delta=0.5,
        metric_noise={"root_cause_accuracy": 0.5},  # 0.1/0.5 = 0.2 <= 0.5
        cost={"cost_penalty": 0.0},
    )
    assert RegularizedSelector().decide(ctx_tight).action == "REVERT"


def test_rule4_small_cost_increase_with_big_gain_passes() -> None:
    # token 微增 (400 -> 404, relative 0.01) + 分数大增 (gain 0.15) -> passes
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8}, tokens=400),
        cand_stats=_stats(metrics={"root_cause_accuracy": 1.0}, tokens=404),
        cost={"cost_penalty": 0.2},
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "PROMOTE"
    assert d.evidence["relative_cost"] == pytest.approx(0.01)


def test_rule4_doubled_tokens_with_small_gain_rejects() -> None:
    # token 翻倍 + 分数微增 (0.8 -> 0.84, net gain 0.34-... still < 0.2*1.0? )
    # gain = 0.04 - 0.05 < 0 anyway; make delta 0 so gain 0.04 < threshold 0.2
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8}, tokens=400),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.84}, tokens=800),
        noise_delta=0.0,
        cost={"cost_penalty": 0.2},
    )
    d = RegularizedSelector().decide(ctx)
    assert d.action == "REJECT"
    assert d.rule_hit == 4


def test_rule5_large_p_value_blocks_promotion() -> None:
    from rsi.decision.significance import SignificanceTester

    # per-task scores identical -> p = 1.0 -> not significant -> rule 6
    ctx = _ctx(
        base_task_scores=[0.5, 0.5, 0.5, 0.5],
        cand_task_scores=[0.5, 0.5, 0.5, 0.5],
        cost={"cost_penalty": 0.0},
    )
    sel = RegularizedSelector(significance=SignificanceTester())
    d = sel.decide(ctx)
    assert d.action == "ADVISE_MANUAL"
    assert d.rule_hit == 6
    assert d.evidence["p_value"] == pytest.approx(1.0)


def test_rule5_significant_p_value_promotes() -> None:
    from rsi.decision.significance import SignificanceTester

    ctx = _ctx(
        base_task_scores=[0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
        cand_task_scores=[0.9, 0.9, 0.9, 0.9, 0.9, 0.9],
        cost={"cost_penalty": 0.0},
    )
    sel = RegularizedSelector(significance=SignificanceTester())
    d = sel.decide(ctx)
    assert d.action == "PROMOTE"
    assert d.rule_hit == 5
    assert d.evidence["p_value"] < 0.05


def test_rule2_guard_switch_off_disables_check() -> None:
    guards = GuardPolicy(missed_check_worsen="off")
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8, "missed_check": 0.0}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.9, "missed_check": 1.0}),
        guards=guards,
    )
    d = RegularizedSelector().decide(ctx)
    # missed_check guard disabled -> falls through to promote
    assert d.action == "PROMOTE"
    assert d.rule_hit == 5

    # same ctx with the switch back on -> rejected at rule 2
    d_on = RegularizedSelector().decide(_ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8, "missed_check": 0.0}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.9, "missed_check": 1.0}),
    ))
    assert d_on.action == "REJECT"
    assert d_on.rule_hit == 2


def test_rule2_high_risk_switch_off() -> None:
    ctx = _ctx(
        base_stats=_stats(metrics={"root_cause_accuracy": 0.8, "high_risk_actions": 0.0}),
        cand_stats=_stats(metrics={"root_cause_accuracy": 0.95, "high_risk_actions": 2.0}),
        guards=GuardPolicy(high_risk_worsen="off", safety_any_worsen="off"),
    )
    d = RegularizedSelector().decide(ctx)
    assert d.rule_hit != 2
