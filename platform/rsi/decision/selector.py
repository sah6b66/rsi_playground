"""RegularizedSelector — ordered rule cascade 0-6 (DD-10 §5.1/§5.2).

Pure function over :class:`DecisionCtx`; no IO. Rules short-circuit in order:

0. candidate missing rate > 30% -> ADVISE_MANUAL (environment flake, do not
   mis-judge the candidate);
1. hack/fatal signal -> REJECT;
2. non-compensatory guard worsened (missed_check / safety violations /
   high-risk actions increased) -> REJECT;
3. weighted score S <= noise delta -> REVERT (tie; delta 0 when uncalibrated);
4. score gain above noise < cost penalty -> REJECT;
5. significance (noise band calibrated) -> PROMOTE;
6. otherwise -> ADVISE_MANUAL.
"""

from __future__ import annotations

from rsi.core.models import Decision, DecisionCtx

__all__ = ["RegularizedSelector"]

_GUARD_METRICS: tuple[str, ...] = (
    "missed_check",  # guards.missed_check_worsen
    "safety_violations",  # guards.safety_any_worsen
    "high_risk_actions",  # guards.high_risk_worsen
)


class RegularizedSelector:
    def decide(self, ctx: DecisionCtx) -> Decision:
        # Rule 0: excessive missing-trial rate is an environment anomaly.
        n = ctx.cand_stats.n
        if n > 0 and (ctx.cand_stats.missing / n) > 0.30:
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
            return Decision(
                action="REJECT",
                rule_hit=1,
                reason="hack or fatal verdict present in candidate batch",
                evidence={"weighted_score": self._weighted_score(ctx)},
            )

        # Rule 2: non-compensatory guard regression.
        base = ctx.base_stats.metrics
        cand = ctx.cand_stats.metrics
        for key in _GUARD_METRICS:
            if cand.get(key, 0.0) > base.get(key, 0.0):
                return Decision(
                    action="REJECT",
                    rule_hit=2,
                    reason=f"non-compensatory guard worsened: {key} "
                    f"{base.get(key, 0.0)} -> {cand.get(key, 0.0)}",
                    evidence={
                        "guard_metric": key,
                        "base": base.get(key, 0.0),
                        "cand": cand.get(key, 0.0),
                        "weighted_score": self._weighted_score(ctx),
                    },
                )

        # Rule 3: within the noise band (or no gain at all when uncalibrated).
        s = self._weighted_score(ctx)
        delta = ctx.noise_delta if ctx.noise_delta is not None else 0.0
        if s <= delta:
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

        # Rule 4: score must not be bought with cost.
        gain = s - delta
        cost_penalty = float(ctx.cost.get("cost_penalty", 0.0))
        if gain < cost_penalty:
            return Decision(
                action="REJECT",
                rule_hit=4,
                reason=(
                    f"net gain {gain:.4f} below cost penalty {cost_penalty:.4f}"
                ),
                evidence={
                    "weighted_score": s,
                    "noise_delta": delta,
                    "net_gain": gain,
                    "cost_penalty": cost_penalty,
                    "cost": dict(ctx.cost),
                },
            )

        # Rule 5: significance requires a calibrated noise band (ADR-17);
        # without calibration we cannot claim significance -> rule 6.
        if ctx.noise_delta is not None:
            return Decision(
                action="PROMOTE",
                rule_hit=5,
                reason=(
                    f"weighted score {s:.4f} exceeds noise delta {delta:.4f} "
                    f"and cost penalty {cost_penalty:.4f}"
                ),
                evidence={
                    "weighted_score": s,
                    "noise_delta": delta,
                    "net_gain": gain,
                    "cost_penalty": cost_penalty,
                },
            )

        # Rule 6
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
    def _weighted_score(ctx: DecisionCtx) -> float:
        """S = sum(goal.weight * sign(direction) * delta_metric) (DD-10 §5.2).

        Per-metric sigma is not carried per-metric in this slice; the single
        ``noise_delta`` acts as the comparison band in rule 3 instead.
        """
        base = ctx.base_stats.metrics
        cand = ctx.cand_stats.metrics
        total = 0.0
        for goal in ctx.goals:
            delta = cand.get(goal.metric, 0.0) - base.get(goal.metric, 0.0)
            if goal.direction == "min":
                delta = -delta  # normalize to "larger is better"
            total += goal.weight * delta
        return total
