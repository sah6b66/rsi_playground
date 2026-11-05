"""IterationEngine — optimization iteration state machine (DD-12 §4.2/§5.3-§5.5).

The engine owns the iteration state machine
(ATTRIBUTING -> HYPOTHESIZING -> REVIEWING -> APPLYING -> VERIFYING -> DECIDING
-> FINISHED, plus PAUSED/ABORTED/ARCHIVED). Transitions are validated against
``VALID_ITERATION_TRANSITIONS`` (DD-01 §3.11); illegal transitions raise
:class:`InvalidTransitionError` (mapped to HTTP 409, TU-027).

The four human gates (§5.4) support two mechanisms:
  * ``gate_handler`` callback (auto/test mode) — called inline;
  * manual gates (``enable_manual_gates=True``) — the engine PAUSES (with a
    persisted pause record in ``state_store``) and waits on a per-gate
    ``asyncio.Event`` with a 5s polling loop (process-restart resilient via
    the store), woken by the server-side ``gate_*`` methods.

APPLYING optionally drives a real :class:`ChangeApplier` (candidate version
from GitStore), DECIDING records via an injected :class:`DecisionRecorder`,
promotes via GitStore when the decision is PROMOTE, and recalculates the
hypothesis ``hit_rate`` (DD-12 §5.5). A DeterministicCritic repair loop is
capped at ``critic_max_repair_rounds`` (default 5) and consecutive no-gain
rounds trip the convergence stop (``no_gain_rounds``, default 3).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from rsi.core.enums import IterationState, is_valid_transition
from rsi.core.errors import InvalidTransitionError
from rsi.core.models import (
    BatchStats,
    Decision,
    DecisionCtx,
    OptimizationProfile,
    TaskRecord,
)
from rsi.core.naming import new_id
from rsi.log import get_logger

__all__ = ["IterationEngine", "RUNNING_ENGINES"]

logger = get_logger(__name__)

# Running engine instances keyed by iteration_id — the API gate endpoints use
# this registry to bridge to a live engine (D-3); a missing entry simply
# falls back to the store-only behaviour.
RUNNING_ENGINES: dict[str, "IterationEngine"] = dict()

_STATES = (
    "ATTRIBUTING",
    "HYPOTHESIZING",
    "REVIEWING",
    "APPLYING",
    "VERIFYING",
    "DECIDING",
    "PAUSED",
    "ABORTED",
    "ARCHIVED",
    "FINISHED",
)

# gate name -> paused step (DD-12 §5.4: awaiting_* values of current_step)
GATE_STEPS: dict[str, str] = {
    "confirm_direction": "awaiting_direction",
    "hypothesis": "awaiting_hypothesis",
    "review": "awaiting_review",
    "decision": "awaiting_decision",
}

# Gate firing order along the state machine (for step progression).
_GATE_ORDER = (
    "confirm_direction",
    "hypothesis",
    "review",
    "decision",
)

_GATE_POLL_S = 5.0  # asyncio.Event wait slice (DD-12 §5.4 dual-safety polling)


class InMemoryPauseStore:
    """Lightweight PAUSED persistence (D-3 P0): dict keyed by iteration_id.

    The DB-backed implementation lands with F4/F5; the engine only needs
    ``save_pause``/``clear_pause``/``get_pause``.
    """

    def __init__(self) -> None:
        self._paused: dict[str, dict[str, Any]] = {}

    def save_pause(self, iteration_id: str, current_step: str, paused_at: str) -> None:
        self._paused[iteration_id] = {
            "iteration_id": iteration_id,
            "current_step": current_step,
            "paused_at": paused_at,
        }

    def clear_pause(self, iteration_id: str) -> None:
        self._paused.pop(iteration_id, None)

    def get_pause(self, iteration_id: str) -> dict[str, Any] | None:
        return self._paused.get(iteration_id)


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
        change_applier=None,
        critic=None,
        decision_recorder=None,
        gitstore=None,
        state_store: InMemoryPauseStore | None = None,
        enable_manual_gates: bool = False,
        critic_max_repair_rounds: int = 5,
        no_gain_rounds: int = 3,
    ) -> None:
        self.batch_runner = batch_runner
        self.attribution_analyzer = attribution_analyzer
        self.selector = selector
        self.event_bus = event_bus
        self.budget = budget
        self.ckpt = ckpt
        self.gate_handler = gate_handler
        self.change_applier = change_applier
        self.critic = critic
        self.decision_recorder = decision_recorder
        self.gitstore = gitstore
        self.state_store = state_store if state_store is not None else InMemoryPauseStore()
        self.enable_manual_gates = enable_manual_gates
        self.critic_max_repair_rounds = critic_max_repair_rounds
        self.no_gain_rounds = no_gain_rounds
        self.round = 0
        # introspection for tests/ops
        self.last_iteration_id: str | None = None
        self.last_state: str | None = None
        self.current_step: str | None = None
        self.paused_from: str | None = None
        self.converged = False
        self._no_gain_streak = 0
        self._pending_gate: str | None = None
        self._gate_payload: dict[str, Any] = {}
        self._gate_events: dict[str, asyncio.Event] = {
            name: asyncio.Event() for name in GATE_STEPS
        }

    # -- public lifecycle (DD-12 §4.2) ----------------------------------------

    async def start(self) -> str:
        """Begin a new iteration: only legal from idle or a terminal state."""
        if (
            self.last_state is not None
            and self.last_state not in ("FINISHED", "ABORTED", "ARCHIVED")
        ):
            raise InvalidTransitionError(
                f"cannot start: iteration {self.last_iteration_id!r} still "
                f"active in state {self.last_state}",
                context={"from_state": self.last_state},
            )
        iteration_id = new_id("iter")
        self.round = 0
        self.converged = False
        self._no_gain_streak = 0
        self.last_iteration_id = iteration_id
        self.last_state = None
        self.current_step = None
        logger.info("iteration.start", iteration_id=iteration_id)
        return iteration_id

    async def pause(self) -> None:
        """Operator pause: any running state -> PAUSED (persisted)."""
        await self._lifecycle_to("PAUSED", "pause")

    async def resume(self) -> None:
        """PAUSED -> the state it was paused from."""
        if self.last_state != "PAUSED":
            raise InvalidTransitionError(
                f"cannot resume iteration in state {self.last_state!r}",
                context={"from_state": self.last_state},
            )
        target = self.paused_from or "ATTRIBUTING"
        await self._advance(self.last_iteration_id or "", "PAUSED", target)
        self.paused_from = None
        self.state_store.clear_pause(self.last_iteration_id or "")
        logger.info("iteration.resume", iteration_id=self.last_iteration_id, state=target)

    async def abort(self) -> None:
        """Abort from any non-terminal state."""
        await self._lifecycle_to("ABORTED", "abort")

    async def archive(self) -> None:
        """Archive: only from FINISHED or ABORTED (soft delete)."""
        if self.last_state not in ("FINISHED", "ABORTED"):
            raise InvalidTransitionError(
                f"cannot archive iteration in state {self.last_state!r}",
                context={"from_state": self.last_state},
            )
        await self._advance(self.last_iteration_id or "", self.last_state, "ARCHIVED")
        logger.info("iteration.archive", iteration_id=self.last_iteration_id)

    async def _lifecycle_to(self, to: str, action: str) -> None:
        from_ = self.last_state
        if from_ is None or not self._valid(from_, to):
            raise InvalidTransitionError(
                f"cannot {action} iteration in state {from_!r}",
                context={"from_state": from_, "to_state": to},
            )
        await self._advance(self.last_iteration_id or "", from_, to)
        if to == "PAUSED":
            self.paused_from = from_
            self.state_store.save_pause(
                self.last_iteration_id or "",
                self.current_step or "",
                datetime.now(timezone.utc).isoformat(),
            )
        else:
            self.paused_from = None
            self.state_store.clear_pause(self.last_iteration_id or "")
        logger.info(f"iteration.{action}", iteration_id=self.last_iteration_id, to=to)

    # -- server-side human gates (D-3: state + current_step double check) -----

    async def gate_confirm_direction(self, selected: list[str]) -> None:
        await self._submit_gate("confirm_direction", {"selected": list(selected)})

    async def gate_hypothesis(self, hyp: dict[str, Any]) -> str:
        hyp = dict(hyp)
        hyp.setdefault("hypothesis_id", new_id("hyp"))
        await self._submit_gate("hypothesis", {"hypothesis": hyp})
        return str(hyp["hypothesis_id"])

    async def gate_review(self, verdict: str, reasons: list[str] | None = None) -> None:
        await self._submit_gate(
            "review", {"verdict": verdict, "reasons": list(reasons or [])}
        )

    async def gate_decision(self, action: str, reason: str = "") -> None:
        await self._submit_gate("decision", {"action": action, "reason": reason})

    async def _submit_gate(self, name: str, payload: dict[str, Any]) -> None:
        """Validate the gate is actually the one being waited on, then wake."""
        step = GATE_STEPS[name]
        if self.last_state != "PAUSED":
            raise InvalidTransitionError(
                f"gate {name!r} requires paused iteration, got {self.last_state!r}",
                context={"gate": name, "from_state": self.last_state},
            )
        if self._pending_gate != name or self.current_step != step:
            raise InvalidTransitionError(
                f"gate {name!r} expects current_step {step!r}, got "
                f"{self.current_step!r} (pending gate: {self._pending_gate!r})",
                context={
                    "gate": name,
                    "current_step": self.current_step,
                    "required_step": step,
                },
            )
        self._gate_payload[name] = payload
        self._gate_events[name].set()
        logger.info(
            "iteration.gate_submitted",
            iteration_id=self.last_iteration_id,
            gate=name,
        )

    async def _wait_gate(self, name: str) -> dict[str, Any]:
        """Manual-gate wait: asyncio.Event + 5s poll slice (dual safety)."""
        while True:
            if self._gate_events[name].is_set():
                break
            try:
                await asyncio.wait_for(self._gate_events[name].wait(), _GATE_POLL_S)
                break
            except TimeoutError:
                # Polling fallback (D-3): an operator may abort while paused.
                if self.last_state == "ABORTED":
                    raise InvalidTransitionError(
                        f"iteration aborted while waiting at gate {name!r}",
                        context={"gate": name},
                    ) from None
                logger.debug(
                    "iteration.gate_poll", iteration_id=self.last_iteration_id, gate=name
                )
        payload = self._gate_payload.pop(name)
        self._gate_events[name].clear()
        return payload

    async def _manual_gate(self, name: str, iteration_id: str) -> dict[str, Any]:
        """PAUSE at gate *name*, wait for the server-side submission, resume."""
        step = GATE_STEPS[name]
        from_ = self.last_state
        await self._advance(iteration_id, from_, "PAUSED")
        self.paused_from = from_
        self.current_step = step
        self._pending_gate = name
        self.state_store.save_pause(
            iteration_id, step, datetime.now(timezone.utc).isoformat()
        )
        payload = await self._wait_gate(name)
        # resume: PAUSED -> the running state we paused from
        await self._advance(iteration_id, "PAUSED", from_)
        self.paused_from = None
        self._pending_gate = None
        self.state_store.clear_pause(iteration_id)
        return payload

    # -- main loop -------------------------------------------------------------

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
        RUNNING_ENGINES[iteration_id] = self
        try:
            return await self._iterate_body(
                iteration_id,
                from_,
                sut_id,
                profile,
                env,
                tasks,
                config_version_id,
                image_ref,
                baseline_stats,
                failed_runs=failed_runs,
                sut_config=sut_config,
                opt_history=opt_history,
                hypothesis=hypothesis,
                candidate_config_version_id=candidate_config_version_id,
                noise_delta=noise_delta,
            )
        finally:
            RUNNING_ENGINES.pop(iteration_id, None)

    async def _iterate_body(
        self,
        iteration_id: str,
        from_: str | None,
        sut_id: str,
        profile: OptimizationProfile,
        env,
        tasks: list[TaskRecord],
        config_version_id: str,
        image_ref: str,
        baseline_stats: BatchStats | None,
        *,
        failed_runs,
        sut_config,
        opt_history,
        hypothesis,
        candidate_config_version_id,
        noise_delta,
    ) -> dict[str, Any]:
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
        elif self.enable_manual_gates:
            resp = await self._manual_gate("confirm_direction", iteration_id)
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
        elif self.enable_manual_gates:
            resp = await self._manual_gate("hypothesis", iteration_id)
            hyp = dict(resp.get("hypothesis", hyp))

        # 3. REVIEWING (deterministic critic + RepairLoop, then human gate)
        await self._advance(iteration_id, from_, "REVIEWING")
        from_ = "REVIEWING"
        if self.critic is not None:
            critic_reject = self._critic_repair_loop(hyp, profile)
            if critic_reject is not None:
                # RepairLoop exhausted -> hypothesis rejected (DD-12 §5.5)
                hyp["status"] = "rejected"
                decision = Decision(
                    action="REVERT",
                    rule_hit=0,
                    reason=critic_reject,
                    evidence={"repair_rounds": self.critic_max_repair_rounds},
                )
                self._record_decision(
                    iteration_id, decision, baseline_stats, None
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
                    "review": {"verdict": "reject", "mode": "critic"},
                    "candidate_config_version_id": None,
                    "short_circuit": "critic_repair_exhausted",
                }
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
        elif self.enable_manual_gates:
            resp = await self._manual_gate("review", iteration_id)
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
            self._record_decision(iteration_id, decision, baseline_stats, None)
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

        # 4. APPLYING: ChangeApplier produces the real candidate version
        # (T2-3); a None applier keeps the P0 bookkeeping placeholder for
        # backward compatibility with the closed-loop stub rig.
        await self._advance(iteration_id, from_, "APPLYING")
        from_ = "APPLYING"
        if candidate_config_version_id is not None:
            cand_cfg = candidate_config_version_id
        elif self.change_applier is not None:
            cand_cfg = self.change_applier.apply(
                sut_id, hyp, set(profile.enabled_targets or ())
            )
        else:
            cand_cfg = f"{config_version_id}#cand-r{self.round}"
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
            elif self.enable_manual_gates:
                resp = await self._manual_gate("decision", iteration_id)
                action = str(resp.get("action", decision.action.lower()))
                mapping = {
                    "promote": "PROMOTE",
                    "revert": "REVERT",
                    "advise_hold": "ADVISE_MANUAL",
                }
                if action in mapping:
                    decision = Decision(
                        action=mapping[action],
                        rule_hit=decision.rule_hit,
                        reason=str(resp.get("reason", decision.reason)),
                        evidence=decision.evidence,
                    )
        logger.debug(
            "iterate_once.decision",
            action=decision.action,
            rule_hit=decision.rule_hit,
        )

        # decision side effects (T2-3): record + promote + hit_rate recalc
        self._record_decision(iteration_id, decision, baseline_stats, candidate_stats)
        if decision.action == "PROMOTE" and self.gitstore is not None:
            from rsi.versioning.changeset import VersionId

            self.gitstore.promote(VersionId(cand_cfg))
            logger.info("iteration.promote", iteration_id=iteration_id, version=cand_cfg)
        self._calc_hit_rate(hyp, baseline_stats, candidate_stats)
        self._track_convergence(profile, baseline_stats, candidate_stats)

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
            "converged": self.converged,
        }

    # -- internals ----------------------------------------------------------------

    def _critic_repair_loop(self, hyp: dict[str, Any], profile) -> str | None:
        """DeterministicCritic + repair retries (DD-12 §5.5 RepairLoop ≤5).

        Returns None when the changeset passes; otherwise a rejection reason
        after ``critic_max_repair_rounds`` failed rounds. Repair itself is
        hypothesis-source work (LLM or human); P0 retries the check and
        rejects when nothing changes.
        """
        files = hyp.get("changeset_files") or []
        if not files:
            return None  # nothing deterministic to check
        for round_i in range(self.critic_max_repair_rounds):
            failures = self.critic.check(
                files, set(profile.enabled_targets or ())
            )
            if not failures:
                return None
            logger.debug(
                "iteration.critic_retry",
                round=round_i + 1,
                failures=failures[:3],
            )
        return (
            f"repair loop exhausted after {self.critic_max_repair_rounds} "
            f"critic rounds: {failures[:1]}"
        )

    def _record_decision(
        self,
        iteration_id: str,
        decision: Decision,
        baseline_stats: BatchStats | None,
        candidate_stats: BatchStats | None,
    ) -> None:
        if self.decision_recorder is None:
            return
        self.decision_recorder.record(
            iteration_id,
            decision,
            f"{iteration_id}-baseline",
            f"{iteration_id}-holdout",
        )

    @staticmethod
    def _calc_hit_rate(
        hyp: dict[str, Any],
        baseline_stats: BatchStats | None,
        candidate_stats: BatchStats | None,
    ) -> None:
        """Recalculate predicted-task hit rate (DD-12 §5.5 calc_hit_rate).

        Per-task scores ride in ``metrics["task_scores"]`` (task_id -> score);
        a predicted task counts as hit only if the candidate improved it.
        """
        predicted = hyp.get("predicted_affected_tasks") or []
        if not predicted:
            return

        def _scores(stats) -> dict:
            if stats is None:
                return {}
            return getattr(stats, "task_scores", None) or {}

        base = _scores(baseline_stats)
        cand = _scores(candidate_stats)
        improved = [
            t
            for t in predicted
            if float(cand.get(t, 0.0)) > float(base.get(t, 0.0))
        ]
        hyp["hit_rate"] = len(improved) / len(predicted)
        hyp["improved_tasks"] = improved

    def _track_convergence(
        self,
        profile: OptimizationProfile,
        baseline_stats: BatchStats | None,
        candidate_stats: BatchStats | None,
    ) -> None:
        """no_gain convergence stop (DD-12 §5.3): N consecutive zero-gain
        rounds trip the converged flag so the driver stops iterating."""
        if not profile.goals:
            return
        metric = str(getattr(profile.goals[0], "metric", "") or "")
        base_v = float((baseline_stats.metrics or {}).get(metric, 0.0)) if baseline_stats else 0.0
        cand_v = float((candidate_stats.metrics or {}).get(metric, 0.0)) if candidate_stats else 0.0
        if cand_v - base_v <= 0:
            self._no_gain_streak += 1
        else:
            self._no_gain_streak = 0
        if self._no_gain_streak >= self.no_gain_rounds:
            self.converged = True
            logger.info(
                "iteration.converged",
                rounds=self._no_gain_streak,
                iteration_id=self.last_iteration_id,
            )

    @staticmethod
    def _valid(from_: str, to: str) -> bool:
        return is_valid_transition(IterationState, from_.lower(), to.lower())

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
                publish_alert = getattr(
                    self.event_bus, "publish_alert", None
                )
                if publish_alert is not None:
                    await publish_alert(
                        "info",
                        "冒烟集未通过，候选版本短路回退",
                        scope=self.last_iteration_id,
                    )
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
        """Validate the transition (TU-027), then publish + checkpoint."""
        if from_ is not None and not self._valid(from_, to):
            raise InvalidTransitionError(
                f"illegal iteration state transition {from_} -> {to}",
                context={
                    "iteration_id": iteration_id,
                    "from_state": from_,
                    "to_state": to,
                },
            )
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
