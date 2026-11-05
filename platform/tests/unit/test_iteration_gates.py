"""Unit tests: IterationEngine human gates + three-tier verification.

Lightweight stub rig (no sandbox/carrier): the batch runner is a stub whose
run_batch() returns pre-set BatchStats, so gate ordering and short-circuit
behavior can be asserted directly (DD-12 §5.4/§5.5).
"""

from __future__ import annotations

import pytest

from rsi.core.models import (
    BatchStats,
    Decision,
    DecisionCtx,
    ExpectedAnswer,
    OptimizationProfile,
    RootCauseExpectation,
    TaskRecord,
    TaskSpec,
)
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.iteration import IterationEngine


def _stats(task_complete: float, rca: float = 0.8) -> BatchStats:
    return BatchStats(
        metrics={"task_complete": task_complete, "root_cause_accuracy": rca},
        n=2,
        missing=0,
        tokens=100,
        tool_calls=2,
        wall_time_s=1.0,
        tokens_estimated=False,
    )


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="fault/gate-01",
        title="gate",
        task=TaskSpec(prompt="p"),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="x")
        ),
    )


def _profile(smoke="es-smoke", iterate="es-iter", holdout="es-hold") -> OptimizationProfile:
    return OptimizationProfile(
        name="gate-profile",
        evalsets={"iterate": iterate, "holdout": holdout, "smoke": smoke},
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        enabled_targets={"system_prompt"},
    )


class StubRunner:
    """run_batch stub: config_version_id -> stats queue (pops per call)."""

    def __init__(self, stats_by_prefix: dict[str, BatchStats]) -> None:
        self.stats_by_prefix = stats_by_prefix
        self.calls: list[str] = []

    async def run_batch(self, batch_id, env, tasks, config_version_id, image_ref, *a, **kw):
        self.calls.append(batch_id)
        for prefix, stats in self.stats_by_prefix.items():
            if batch_id.startswith(prefix):
                return stats
        return _stats(1.0)


class StubAttribution:
    def analyze(self, failed_runs, sut_config, opt_history):
        from rsi.optimizer.attribution import AttributionAnalyzer

        return AttributionAnalyzer().analyze(failed_runs, sut_config, opt_history)


class RecordingSelector:
    def __init__(self) -> None:
        self.ctx: DecisionCtx | None = None

    def decide(self, ctx: DecisionCtx) -> Decision:
        self.ctx = ctx
        return Decision(action="PROMOTE", rule_hit=5, reason="stub", evidence={})


class StubBudget:
    async def record(self, scope, tokens, cost, wall_s) -> None:
        pass


def _engine(runner, gate_handler=None):
    sel = RecordingSelector()
    eng = IterationEngine(
        batch_runner=runner,
        attribution_analyzer=StubAttribution(),
        selector=sel,
        event_bus=EventBus(),
        budget=StubBudget(),
        gate_handler=gate_handler,
    )
    return eng, sel


@pytest.mark.asyncio
async def test_gate_handler_called_at_all_four_gates():
    runner = StubRunner({"verify-holdout": _stats(1.0)})
    gate_calls: list[tuple[str, dict]] = []

    async def gates(name: str, ctx: dict) -> dict:
        gate_calls.append((name, ctx))
        if name == "confirm_direction":
            return {"selected": ["c1"]}
        if name == "hypothesis":
            return {"hypothesis": {"summary": "human hypothesis"}}
        if name == "review":
            return {"verdict": "pass", "reasons": []}
        return {"action": "promote", "reason": "ok"}

    eng, sel = _engine(runner, gates)
    result = await eng.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0, 0.4),
        candidate_config_version_id="cfg-v2",
    )
    assert [n for n, _ in gate_calls] == [
        "confirm_direction", "hypothesis", "review", "decision",
    ]
    # handler-authored hypothesis replaced the pre-built one
    assert result["hypothesis"]["summary"] == "human hypothesis"
    assert result["review"]["verdict"] == "pass"
    assert result["review"]["mode"] == "human-gate"
    assert result["decision"].action == "PROMOTE"
    assert sel.ctx is not None  # selector still ran and produced a suggestion


@pytest.mark.asyncio
async def test_gate_review_reject_short_circuits_to_revert():
    runner = StubRunner({})

    async def gates(name: str, ctx: dict) -> dict:
        if name == "review":
            return {"verdict": "reject", "reasons": ["unsafe change"]}
        return {"selected": [], "hypothesis": {}, "action": "promote"}

    eng, _ = _engine(runner, gates)
    result = await eng.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0),
    )
    assert result["decision"].action == "REVERT"
    assert result["short_circuit"] == "review_gate"
    assert runner.calls == []  # no verification batches spent


@pytest.mark.asyncio
async def test_gate_decision_can_override_selector_to_revert():
    runner = StubRunner({"verify-holdout": _stats(1.0)})

    async def gates(name: str, ctx: dict) -> dict:
        if name == "decision":
            assert ctx["selector_suggestion"]["action"] == "PROMOTE"
            return {"action": "advise_hold", "reason": "human says wait"}
        return {"selected": [], "hypothesis": {}, "verdict": "pass"}

    eng, _ = _engine(runner, gates)
    result = await eng.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0),
    )
    assert result["decision"].action == "ADVISE_MANUAL"
    assert result["decision"].reason == "human says wait"


@pytest.mark.asyncio
async def test_three_tier_smoke_fail_short_circuits_revert():
    runner = StubRunner(
        {"verify-smoke": _stats(0.2), "verify-screen": _stats(0.9), "verify-holdout": _stats(0.9)}
    )
    eng, sel = _engine(runner)
    result = await eng.iterate_once(
        "sut-1", _profile(smoke="es-smoke"), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0),
        candidate_config_version_id="cfg-bad",
    )
    assert result["short_circuit"] == "smoke"
    assert result["decision"].action == "REVERT"
    # only the smoke tier ran; screen and holdout skipped
    assert runner.calls == ["verify-smoke-es-smoke"]
    assert sel.ctx is None  # selector never consulted on short-circuit


@pytest.mark.asyncio
async def test_three_tier_screen_fail_short_circuits_revert():
    runner = StubRunner(
        {"verify-smoke": _stats(0.9), "verify-screen": _stats(0.1), "verify-holdout": _stats(0.9)}
    )
    eng, _ = _engine(runner)
    result = await eng.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0),
    )
    assert result["short_circuit"] == "screen"
    assert result["decision"].action == "REVERT"
    assert runner.calls == ["verify-smoke-es-smoke", "verify-screen-es-iter"]


@pytest.mark.asyncio
async def test_three_tier_all_pass_holdout_feeds_decision():
    runner = StubRunner(
        {"verify-smoke": _stats(1.0), "verify-screen": _stats(0.9), "verify-holdout": _stats(1.0, 0.95)}
    )
    eng, sel = _engine(runner)
    result = await eng.iterate_once(
        "sut-1", _profile(), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0, 0.4),
        candidate_config_version_id="cfg-v2",
    )
    assert result["short_circuit"] is None
    assert runner.calls == [
        "verify-smoke-es-smoke", "verify-screen-es-iter", "verify-holdout-es-hold",
    ]
    # the DECIDING gate consumed the holdout stats
    assert sel.ctx is not None
    assert sel.ctx.cand_stats.metrics["root_cause_accuracy"] == pytest.approx(0.95)
    assert result["decision"].action == "PROMOTE"


@pytest.mark.asyncio
async def test_unconfigured_evalsets_go_straight_to_holdout():
    runner = StubRunner({"verify-holdout": _stats(1.0)})
    eng, _ = _engine(runner)
    result = await eng.iterate_once(
        "sut-1", _profile(smoke="", iterate=""), None, [_task()], "cfg-v1", "img",
        baseline_stats=_stats(1.0),
    )
    assert runner.calls == ["verify-holdout-es-hold"]
    assert result["short_circuit"] is None
