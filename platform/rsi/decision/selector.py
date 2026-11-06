"""RegularizedSelector — ordered rule cascade 0-6 (DD-10 §5.1/§5.2).

Pure function over :class:`DecisionCtx`; no IO. Rules short-circuit in order:

0. candidate missing rate > 30% -> ADVISE_MANUAL (environment flake, do not
   mis-judge the candidate);
1. hack/fatal signal -> REJECT;
2. non-compensatory guard worsened (missed_check / safety violations /
   high-risk actions increased) -> REJECT — each guard is individually
   switchable via ``GuardPolicy`` (``off`` disables that check);
3. weighted score S <= noise delta -> REVERT (tie; delta 0 when uncalibrated);
4. relative cost rule: net gain below ``cost_penalty * relative_cost`` where
   ``relative_cost = |Δtokens|/base_tokens + |Δcalls|/base_calls +
   |Δtime|/base_time`` (DD-10 §5.1 rule 4) -> REJECT;
5. significance (paired test, p < alpha) -> PROMOTE; requires a calibrated
   noise band and an injected :class:`SignificanceTester` (ADR-17);
6. otherwise -> ADVISE_MANUAL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rsi.core.models import Decision, DecisionCtx, GuardPolicy
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rsi.decision.significance import SignificanceTester

__all__ = ["RegularizedSelector"]

logger = get_logger(__name__)

# GuardPolicy switch (D-7) -> batch metric it watches.
_GUARD_METRICS: tuple[tuple[str, str], ...] = (
    ("missed_check_worsen", "missed_check"),
    ("safety_any_worsen", "safety_violations"),
    ("high_risk_worsen", "high_risk_actions"),
)


def _relative_ratio(delta: float, base: float) -> float:
    """|delta| / base with zero-base guards (0/0 -> 0; Δ≠0 over 0 -> huge)."""
    delta = abs(float(delta))
    base = float(base)
    if base > 0:
        return delta / base
    return 0.0 if delta == 0.0 else 1e9


class RegularizedSelector:
    def __init__(
        self,
        significance: "SignificanceTester | None" = None,
        alpha: float = 0.05,
    ) -> None:
        # D-7 rule 5: paired significance on per-task scores. ``None`` keeps
        # the pre-D-7 behaviour (calibrated band alone -> PROMOTE).
        self.significance = significance
        self.alpha = alpha

    def decide(self, ctx: DecisionCtx) -> Decision:
        logger.debug(
            "decide.entry",
            cand_n=ctx.cand_stats.n,
            cand_missing=ctx.cand_stats.missing,
            hack_or_fatal=ctx.hack_or_fatal,
        )
        # Rule 0: excessive missing-trial rate is an environment anomaly.
        n = ctx.cand_stats.n
        if n > 0 and (ctx.cand_stats.missing / n) > 0.30:
            logger.debug("decide.exit", rule_hit=0, action="ADVISE_MANUAL")
            return Decision(
                action="ADVISE_MANUAL",
                rule_hit=0,
                reason=(
                    f"缺试率 {ctx.cand_stats.missing / n:.0%}，"
                    "环境异常不影响判定"
                ),
                evidence={
                    "missing": ctx.cand_stats.missing,
                    "n": n,
                    "weighted_score": self._weighted_score(ctx),
                },
            )

        # Rule 1: hack or fatal verdict anywhere in the candidate batch.
        if ctx.hack_or_fatal:
            logger.debug("decide.exit", rule_hit=1, action="REJECT")
            return Decision(
                action="REJECT",
                rule_hit=1,
                reason="hack or fatal verdict present in candidate batch",
                evidence={"weighted_score": self._weighted_score(ctx)},
            )

        # Rule 2: non-compensatory guard regression (per GuardPolicy switch).
        base = ctx.base_stats.metrics
        cand = ctx.cand_stats.metrics
        for switch, metric in _GUARD_METRICS:
            if self._guard_enabled(ctx.guards, switch):
                if cand.get(metric, 0.0) > base.get(metric, 0.0):
                    logger.debug(
                        "decide.exit", rule_hit=2, action="REJECT", guard_metric=metric
                    )
                    return Decision(
                        action="REJECT",
                        rule_hit=2,
                        reason=f"non-compensatory guard worsened: {metric} "
                        f"{base.get(metric, 0.0)} -> {cand.get(metric, 0.0)}",
                        evidence={
                            "guard_metric": metric,
                            "guard_switch": switch,
                            "base": base.get(metric, 0.0),
                            "cand": cand.get(metric, 0.0),
                            "weighted_score": self._weighted_score(ctx),
                        },
                    )

        # Rule 3: within the noise band (or no gain at all when uncalibrated).
        s = self._weighted_score(ctx)
        delta = ctx.noise_delta if ctx.noise_delta is not None else 0.0
        if s <= delta:
            logger.debug("decide.exit", rule_hit=3, action="REVERT")
            return Decision(
                action="REVERT",
                rule_hit=3,
                reason=(
                    f"weighted score {s:.4f} within noise delta {delta:.4f}"
                    if ctx.noise_delta is not None
                    else f"weighted score {s:.4f} not above uncalibrated delta 0"
                ),
                evidence={"weighted_score": s, "noise_delta": delta},
            )

        # Rule 4 (D-7): relative-cost normalization (DD-10 §5.1 rule 4).
        #   reject when gain < cost_penalty * (|Δtokens|/base_tokens +
        #   |Δcalls|/base_calls + |Δtime|/base_time); bases come from the
        #   baseline batch stats.
        gain = s - delta
        cost_penalty = float(ctx.cost.get("cost_penalty", 0.0))
        relative_cost = self._relative_cost(ctx)
        cost_threshold = cost_penalty * relative_cost
        if gain < cost_threshold:
            logger.debug("decide.exit", rule_hit=4, action="REJECT")
            return Decision(
                action="REJECT",
                rule_hit=4,
                reason=(
                    f"net gain {gain:.4f} below relative cost penalty "
                    f"{cost_threshold:.4f} (relative cost {relative_cost:.4f})"
                ),
                evidence={
                    "weighted_score": s,
                    "noise_delta": delta,
                    "net_gain": gain,
                    "cost_penalty": cost_penalty,
                    "relative_cost": relative_cost,
                    "cost": dict(ctx.cost),
                },
            )

        # Rule 5: significance requires a calibrated noise band (ADR-17);
        # without calibration we cannot claim significance -> rule 6.
        if ctx.noise_delta is not None:
            sig = self._significance_result(ctx)
            if sig is not None:
                p_value, significant, method = sig
                if not significant:
                    logger.debug(
                        "decide.exit", rule_hit=6, action="ADVISE_MANUAL",
                        p_value=p_value,
                    )
                    return Decision(
                        action="ADVISE_MANUAL",
                        rule_hit=6,
                        reason=(
                            f"paired {method} p={p_value:.4f} >= alpha "
                            f"{self.alpha:.2f}: gain not statistically significant"
                        ),
                        evidence={
                            "weighted_score": s,
                            "net_gain": gain,
                            "p_value": p_value,
                            "alpha": self.alpha,
                            "method": method,
                        },
                    )
            logger.debug("decide.exit", rule_hit=5, action="PROMOTE")
            return Decision(
                action="PROMOTE",
                rule_hit=5,
                reason=(
                    f"weighted score {s:.4f} exceeds noise delta {delta:.4f} "
                    f"and relative cost penalty {cost_threshold:.4f}"
                ),
                evidence={
                    "weighted_score": s,
                    "noise_delta": delta,
                    "net_gain": gain,
                    "cost_penalty": cost_penalty,
                    "relative_cost": relative_cost,
                    **(
                        {"p_value": sig[0], "method": sig[2]}
                        if sig is not None
                        else {}
                    ),
                },
            )

        # Rule 6
        logger.debug("decide.exit", rule_hit=6, action="ADVISE_MANUAL")
        return Decision(
            action="ADVISE_MANUAL",
            rule_hit=6,
            reason=(
                "noise band uncalibrated: gain is positive but significance "
                "cannot be established"
            ),
            evidence={
                "weighted_score": s,
                "net_gain": gain,
                "cost_penalty": cost_penalty,
            },
        )

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _guard_enabled(guards: GuardPolicy, switch: str) -> bool:
        return str(getattr(guards, switch, "reject")) != "off"

    @staticmethod
    def _weighted_score(ctx: DecisionCtx) -> float:
        """S = Σ wᵢ × (±Δmᵢ / σᵢ) (DD-10 §5.2, D-7).

        σᵢ comes from ``ctx.metric_noise`` when calibrated; 1.0 otherwise
        (uncalibrated degradation keeps legacy behaviour for single goals).
        ``min`` direction metrics flip the delta sign so larger is better.
        """
        base = ctx.base_stats.metrics
        cand = ctx.cand_stats.metrics
        noise = ctx.metric_noise or {}
        total = 0.0
        for goal in ctx.goals:
            delta = cand.get(goal.metric, 0.0) - base.get(goal.metric, 0.0)
            if goal.direction == "min":
                delta = -delta  # normalize to "larger is better"
            sigma = float(noise.get(goal.metric, 1.0))
            if sigma <= 0.0:
                sigma = 1.0
            total += goal.weight * (delta / sigma)
        return total

    @staticmethod
    def _relative_cost(ctx: DecisionCtx) -> float:
        """|Δtokens|/base_tokens + |Δcalls|/base_calls + |Δtime|/base_time."""
        base, cand = ctx.base_stats, ctx.cand_stats
        return (
            _relative_ratio(cand.tokens - base.tokens, base.tokens)
            + _relative_ratio(cand.tool_calls - base.tool_calls, base.tool_calls)
            + _relative_ratio(cand.wall_time_s - base.wall_time_s, base.wall_time_s)
        )

    def _significance_result(
        self, ctx: DecisionCtx
    ) -> tuple[float, bool, str] | None:
        """Run the paired test when tester + per-task scores are available."""
        if self.significance is None:
            return None
        base = ctx.base_task_scores
        cand = ctx.cand_task_scores
        if not base or not cand:
            return None
        if len(base) != len(cand):
            return None
        res = self.significance.test(list(base), list(cand), alpha=self.alpha)
        return res.p_value, res.significant, res.method
