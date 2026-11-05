"""IterationEngine — optimization iteration state machine (DD-12 §4.2/§5.3-§5.5).

P0 scope: the engine demonstrates the STATE MACHINE (ATTRIBUTING ->
HYPOTHESIZING -> REVIEWING -> APPLYING -> VERIFYING -> DECIDING -> FINISHED)
and the DATA FLOW (attribution -> hypothesis -> verification batches ->
DecisionCtx -> Decision). The four human gates (§5.4) are auto-accepted in
P0: a pre-built hypothesis is accepted as-is, review is accepted as pass,
and the candidate config version is assumed to already exist (APPLYING is
bookkeeping only). Real LLM-driven optimization arrives in a later plan.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from rsi.core.models import (
    BatchStats,
    Decision,
    DecisionCtx,
    OptimizationProfile,
    TaskRecord,
)
from rsi.core.naming import new_id
from rsi.log import get_logger

__all__ = ["IterationEngine"]

logger = get_logger(__name__)

_STATES = (
    "ATTRIBUTING",
    "HYPOTHESIZING",
    "REVIEWING",
    "APPLYING",
    "VERIFYING",
    "DECIDING",
    "FINISHED",
)


class IterationEngine:
    """Optimization iteration state machine with human gates (P0/P1 mode)."""

    GateHandler = Callable[[str, dict], Awaitable[dict]]

    def __init__(
        self,
        batch_runner,
        attribution_analyzer,
        selector,
        event_bus,
        budget,
        *,
        ckpt=None,
        gate_handler: GateHandler | None = None,
    ) -> None:
        self.batch_runner = batch_runner
        self.attribution_analyzer = attribution_analyzer
        self.selector = selector
        self.event_bus = event_bus
        self.budget = budget
        self.ckpt = ckpt
        self.gate_handler = gate_handler
        self.round = 0
        # introspection for tests/ops
        self.last_iteration_id: str | None = None
        self.last_state: str | None = None

    async def iterate_once(
        self,
        sut_id: str,
        profile: OptimizationProfile,
        env,
        tasks: list[TaskRecord],
        config_version_id: str,
        image_ref: str,
        baseline_stats: BatchStats | None = None,
        *,
        failed_runs: list[dict[str, Any]] | None = None,
        sut_config: dict[str, str] | None = None,
        opt_history: list[dict[str, Any]] | None = None,
        hypothesis: dict[str, Any] | None = None,
        candidate_config_version_id: str | None = None,
        noise_delta: float | None = None,
    ) -> dict[str, Any]:
        """One iteration round (P0): state machine + data flow, no real LLM."""
        logger.debug(
            "iterate_once.entry",
            sut_id=sut_id,
            profile=profile.name,
            tasks=len(tasks),
            round=self.round + 1,
        )
        self.round += 1
        iteration_id = new_id("iter")
        self.last_iteration_id = iteration_id
        from_ = None

        # 1. ATTRIBUTING: rule-layer analysis of the baseline failures
        await self._advance(iteration_id, from_, "ATTRIBUTING")
        from_ = "ATTRIBUTING"
        report = self.attribution_analyzer.analyze(
            failed_runs or [], sut_config or {}, opt_history or []
        )

        # Gate 1: ATTRIBUTING -> HYPOTHESIZING (confirm failure direction)
        selected_clusters: list[str] | None = None
        if self.gate_handler is not None:
            resp = await self.gate_handler(
                "confirm_direction", {"clusters": report.clusters}
            )
            selected_clusters = list(resp.get("selected", []))

        # 2. HYPOTHESIZING (human gate: hypothesis authoring/approval)
        await self._advance(iteration_id, from_, "HYPOTHESIZING")
        from_ = "HYPOTHESIZING"
        hyp: dict[str, Any] = dict(
            hypothesis
            if hypothesis is not None
            else {
                "summary": (
                    f"P0 pre-built hypothesis from "
                    f"{len(report.clusters)} cluster(s)"
                ),
                "clusters": [
                    c.failure_mode for c in report.clusters
                ],
            }
        )
        if selected_clusters is not None:
            hyp["selected_clusters"] = selected_clusters
        if self.gate_handler is not None:
            resp = await self.gate_handler(
                "hypothesis", {"problem_statement": hyp.get("summary", "")}
            )
            hyp = dict(resp.get("hypothesis", hyp))

        # 3. REVIEWING (human gate: changeset/det review)
        await self._advance(iteration_id, from_, "REVIEWING")
        from_ = "REVIEWING"
        review = {"verdict": "pass", "mode": "p0-auto"}
        if self.gate_handler is not None:
            resp = await self.gate_handler(
                "review",
                {
                    "changeset": hyp.get("changeset"),
                    "det_results": hyp.get("det_results"),
                },
            )
            review = {
                "verdict": resp.get("verdict", "pass"),
                "reasons": list(resp.get("reasons", [])),
                "mode": "human-gate",
            }
            if review["verdict"] != "pass":
                # rejected at review: short-circuit REVERT without verification
                decision = Decision(
                    action="REVERT",
                    rule_hit=0,
                    reason="review gate rejected the changeset",
                    evidence={"reasons": review["reasons"]},
                )
                await self._advance(iteration_id, from_, "DECIDING")
                await self._advance(iteration_id, "DECIDING", "FINISHED")
                self.last_state = "FINISHED"
                return {
                    "round": self.round,
                    "iteration_id": iteration_id,
                    "decision": decision,
                    "baseline_stats": baseline_stats,
                    "candidate_stats": None,
                    "attribution_report": report,
                    "hypothesis": hyp,
                    "review": review,
                    "candidate_config_version_id": None,
                    "short_circuit": "review_gate",
                }

        # 4. APPLYING (P0: candidate config version assumed to exist)
        await self._advance(iteration_id, from_, "APPLYING")
        from_ = "APPLYING"
        cand_cfg = (
            candidate_config_version_id
            if candidate_config_version_id is not None
            else f"{config_version_id}#cand-r{self.round}"
        )
        hyp["status"] = "applied"

        # 5. VERIFYING: baseline (if not supplied) + three-tier candidate
        await self._advance(iteration_id, from_, "VERIFYING")
        from_ = "VERIFYING"
        if baseline_stats is None:
            baseline_stats = await self.batch_runner.run_batch(
                f"{iteration_id}-baseline",
                env,
                tasks,
                config_version_id,
                image_ref,
            )
        candidate_stats, short_circuit = await self._verifying_phase(
            env, tasks, cand_cfg, image_ref, profile
        )

        # 6. DECIDING: DecisionCtx -> selector -> human gate -> Decision
        await self._advance(iteration_id, from_, "DECIDING")
        from_ = "DECIDING"
        if short_circuit is not None:
            decision = Decision(
                action="REVERT",
                rule_hit=0,
                reason=f"short-circuit revert at {short_circuit} tier",
                evidence={"tier": short_circuit, "stats": candidate_stats.metrics},
            )
        else:
            ctx = DecisionCtx(
                base_stats=baseline_stats,
                cand_stats=candidate_stats,
                noise_delta=noise_delta,
                goals=profile.goals,
                guards=profile.guards,
            )
            decision = self.selector.decide(ctx)
            if self.gate_handler is not None:
                resp = await self.gate_handler(
                    "decision",
                    {"selector_suggestion": decision.model_dump(mode="json")},
                )
                action = resp.get("action", decision.action.lower())
                mapping = {
                    "promote": "PROMOTE",
                    "revert": "REVERT",
                    "advise_hold": "ADVISE_MANUAL",
                }
                if action in mapping:
                    decision = Decision(
                        action=mapping[action],
                        rule_hit=decision.rule_hit,
                        reason=resp.get("reason", decision.reason),
                        evidence=decision.evidence,
                    )
        logger.debug(
            "iterate_once.decision",
            action=decision.action,
            rule_hit=decision.rule_hit,
        )

        await self._advance(iteration_id, from_, "FINISHED")
        self.last_state = "FINISHED"

        # budget: record combined usage under the iteration scope
        await self.budget.record(
            iteration_id,
            baseline_stats.tokens + candidate_stats.tokens,
            0.0,
            baseline_stats.wall_time_s + candidate_stats.wall_time_s,
        )

        logger.debug(
            "iterate_once.exit",
            iteration_id=iteration_id,
            decision=decision.action,
            clusters=len(report.clusters),
        )
        return {
            "round": self.round,
            "iteration_id": iteration_id,
            "decision": decision,
            "baseline_stats": baseline_stats,
            "candidate_stats": candidate_stats,
            "attribution_report": report,
            "hypothesis": hyp,
            "review": review,
            "candidate_config_version_id": cand_cfg,
            "short_circuit": short_circuit,
        }

    # -- internals ----------------------------------------------------------------

    async def _verifying_phase(
        self,
        env,
        tasks: list[TaskRecord],
        config_version_id: str,
        image_ref: str,
        profile: OptimizationProfile,
    ) -> tuple[BatchStats, str | None]:
        """Three-tier candidate verification with short-circuit (DD-12 §5.5).

        Tier 1 smoke: task_complete rate < 0.5 -> SHORT_CIRCUIT_REVERT.
        Tier 2 iterate screen: task_complete rate < 0.5 -> SHORT_CIRCUIT_REVERT.
        Tier 3 holdout: always runs; stats returned for the DECIDING gate.
        Tiers 1-2 are skipped when the evalset is not configured.
        """
        ev = profile.evalsets
        smoke = getattr(ev, "smoke", "") or ""
        iterate = getattr(ev, "iterate", "") or ""

        if smoke:
            stats = await self.batch_runner.run_batch(
                f"verify-smoke-{smoke}", env, tasks, config_version_id, image_ref
            )
            rate = stats.metrics.get("task_complete", 0.0)
            logger.info("verify.smoke", task_complete_rate=rate, n=stats.n)
            if rate < 0.5:
                return stats, "smoke"

        if iterate:
            stats = await self.batch_runner.run_batch(
                f"verify-screen-{iterate}", env, tasks, config_version_id, image_ref
            )
            rate = stats.metrics.get("task_complete", 0.0)
            logger.info("verify.screen", task_complete_rate=rate, n=stats.n)
            if rate < 0.5:
                return stats, "screen"

        holdout = getattr(ev, "holdout", "") or ""
        stats = await self.batch_runner.run_batch(
            f"verify-holdout-{holdout}", env, tasks, config_version_id, image_ref
        )
        logger.info(
            "verify.holdout",
            task_complete_rate=stats.metrics.get("task_complete", 0.0),
            n=stats.n,
        )
        return stats, None

    async def _advance(self, iteration_id: str, from_: str | None, to: str) -> None:
        """Publish iteration.status_changed and checkpoint the state."""
        self.last_state = to
        await self.event_bus.publish("iteration", iteration_id, from_ or "", to)
        if self.ckpt is not None:
            await self.ckpt.save(
                f"iter-{iteration_id}",
                {
                    "iteration_id": iteration_id,
                    "round": self.round,
                    "state": to,
                    "from": from_,
                },
            )
