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

from typing import Any

from rsi.core.models import (
    BatchStats,
    DecisionCtx,
    OptimizationProfile,
    TaskRecord,
)
from rsi.core.naming import new_id

__all__ = ["IterationEngine"]

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

    def __init__(
        self,
        batch_runner,
        attribution_analyzer,
        selector,
        event_bus,
        budget,
        *,
        ckpt=None,
    ) -> None:
        self.batch_runner = batch_runner
        self.attribution_analyzer = attribution_analyzer
        self.selector = selector
        self.event_bus = event_bus
        self.budget = budget
        self.ckpt = ckpt
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

        # 2. HYPOTHESIZING (P0 human gate: accept the pre-built hypothesis)
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

        # 3. REVIEWING (P0 human gate: accept pass)
        await self._advance(iteration_id, from_, "REVIEWING")
        from_ = "REVIEWING"
        review = {"verdict": "pass", "mode": "p0-auto"}

        # 4. APPLYING (P0: candidate config version assumed to exist)
        await self._advance(iteration_id, from_, "APPLYING")
        from_ = "APPLYING"
        cand_cfg = (
            candidate_config_version_id
            if candidate_config_version_id is not None
            else f"{config_version_id}#cand-r{self.round}"
        )
        hyp["status"] = "applied"

        # 5. VERIFYING: baseline (if not supplied) + candidate batch
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
        candidate_stats = await self.batch_runner.run_batch(
            f"{iteration_id}-cand",
            env,
            tasks,
            cand_cfg,
            image_ref,
        )

        # 6. DECIDING: DecisionCtx -> selector -> Decision
        await self._advance(iteration_id, from_, "DECIDING")
        from_ = "DECIDING"
        ctx = DecisionCtx(
            base_stats=baseline_stats,
            cand_stats=candidate_stats,
            noise_delta=noise_delta,
            goals=profile.goals,
            guards=profile.guards,
        )
        decision = self.selector.decide(ctx)

        await self._advance(iteration_id, from_, "FINISHED")
        self.last_state = "FINISHED"

        # budget: record combined usage under the iteration scope
        await self.budget.record(
            iteration_id,
            baseline_stats.tokens + candidate_stats.tokens,
            0.0,
            baseline_stats.wall_time_s + candidate_stats.wall_time_s,
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
        }

    # -- internals ----------------------------------------------------------------

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
