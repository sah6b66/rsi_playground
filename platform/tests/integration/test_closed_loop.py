"""Closed-loop integration: baseline batch -> IterationEngine -> Decision.

Full offline chain (DD-12 IN-001/IN-006 slice): FakeSandboxBackend +
FakeCarrier + real TraceCollector/TraceStore + mock judge (verdict keyed by
config version) + real BatchRunner + real IterationEngine + real
AttributionAnalyzer/RegularizedSelector/EventBus/BudgetController/
CheckpointStore. No LLM calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.enums import EventType
from rsi.core.models import (
    CliEntry,
    Decision,
    DeviceSpec,
    Efficiency,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    OptimizationProfile,
    PathQuality,
    PrepareAction,
    RootCauseExpectation,
    SafetyVerdict,
    Scores,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
    Verdict,
)
from rsi.decision.aggregator import StatsAggregator
from rsi.decision.selector import RegularizedSelector
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.iteration import IterationEngine
from rsi.optimizer.attribution import AttributionAnalyzer
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
GOOD = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown", "confidence": 0.9}'
OUT = f"working...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


def _env() -> EnvSpec:
    return EnvSpec(
        name="lab-closed",
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
    )


def _task(n: int) -> TaskRecord:
    return TaskRecord(
        task_id=f"fault/loop-{n:02d}",
        title=f"closed-loop task {n}",
        prepare=[
            PrepareAction(
                action="exec_cli",
                entries=[
                    CliEntry(device="R1", cli_lines=["interface GE0/0/1", "shutdown"])
                ],
            )
        ],
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
        ),
    )


def _verdict(rca: float) -> Verdict:
    return Verdict(
        scores=Scores(
            root_cause_accuracy=rca,
            task_complete=True,
            diagnostic_steps=3,
            missed_check=0,
            false_positive=0,
        ),
        path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
        efficiency=Efficiency(tokens=120, tool_calls=2, wall_time_s=1.5),
        safety=SafetyVerdict(),
        missing=False,
    )


class ClosedLoopRig:
    """Full offline stack: sandbox/carrier/tracing/judge/batch/iteration."""

    def __init__(self, tmp_path: Path, rca_by_config: dict[str, float]) -> None:
        self.backend = FakeSandboxBackend()
        self.sandbox = SandboxService(backends={"fake": self.backend}, labs_dir=tmp_path)
        self.carrier = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
        )
        self.judge = _BatchAwareJudge(rca_by_config)
        self.collector = TraceCollector(SeqAllocator(), None)
        self.trace_store = TraceStore(self.collector)
        self.ckpt = CheckpointStore()
        self.budget = BudgetController(
            stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
        )
        self.event_bus = EventBus()
        self.runner = BatchRunner(
            sandbox=self.sandbox,
            carrier=self.carrier,
            collector=self.collector,
            trace_store=self.trace_store,
            judge=self.judge,
            ckpt=self.ckpt,
            budget=self.budget,
            event_bus=self.event_bus,
            stats_agg=StatsAggregator(),
        )
        self.engine = IterationEngine(
            batch_runner=self.runner,
            attribution_analyzer=AttributionAnalyzer(),
            selector=RegularizedSelector(),
            event_bus=self.event_bus,
            budget=self.budget,
            ckpt=self.ckpt,
        )


class _BatchAwareJudge:
    """Judge mock whose verdict depends on the run's config_version_id.

    BatchRunner forwards config_version_id to judge_run (T2-5), so the
    verdict is selected directly from the run context; the queue-based
    workaround is kept only as a fallback for callers that pass no context.
    """

    def __init__(self, rca_by_config: dict[str, float]) -> None:
        self.rca_by_config = rca_by_config
        self._pending: list[float] = []
        self.calls = 0

    def set_batch(self, config_version_id: str, n: int) -> None:
        self._pending = [self.rca_by_config[config_version_id]] * n

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        cfg = kw.get("config_version_id")
        if cfg in self.rca_by_config:
            rca = self.rca_by_config[cfg]
        else:
            rca = self._pending[self.calls % len(self._pending)] if self._pending else 0.0
        self.calls += 1
        return _verdict(rca)


def _profile() -> OptimizationProfile:
    return OptimizationProfile(
        name="p0-profile",
        evalsets={"iterate": "eval-iter", "holdout": "eval-hold", "smoke": "eval-smoke"},
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        enabled_targets={"system_prompt"},
    )


def _failed_run(run_id: str, rca: float) -> dict:
    from rsi.core.models import TraceEvent

    return {
        "run_id": run_id,
        "events": [
            TraceEvent(
                run_id=run_id,
                task_id="fault/loop-01",
                lab_id="lab-x",
                sut_id="sut-1",
                seq=0,
                ts="2026-11-04T10:00:00Z",
                event_type=EventType.TOOL_CALL,
                data={"args": {"command": "display current-configuration"}},
            )
        ],
        "verdict": _verdict(rca),
    }


@pytest.mark.asyncio
async def test_batch_lab_journal_single_snap0(tmp_path: Path) -> None:
    """F6/R5：新建 lab 路径 build_lab 已内置 snap0 并落 ops.jsonl——
    BatchRunner 不得再补拍（S11 实测每个 lab 出现两条 snap0 记录）。"""
    rig = ClosedLoopRig(tmp_path, {"cfg-v1": 0.5})
    env, tasks = _env(), [_task(1)]
    rig.judge.set_batch("cfg-v1", len(tasks))
    await rig.runner.run_batch("batch-r5", env, tasks, "cfg-v1", "sut-img")

    import json

    journals = list(tmp_path.glob("lab-*/ops.jsonl"))
    assert journals, "lab ops journal expected"
    lines = [
        json.loads(line)
        for line in journals[0].read_text(encoding="utf-8").splitlines()
    ]
    snaps = [l for l in lines if l.get("record") == "snapshot"]
    assert len(snaps) == 1, f"expected exactly one snapshot record, got {snaps}"


@pytest.mark.asyncio
async def test_closed_loop_promote(tmp_path: Path) -> None:
    rig = ClosedLoopRig(tmp_path, {"cfg-v1": 0.4, "cfg-v2": 0.9})
    env, tasks = _env(), [_task(1), _task(2)]
    q = await rig.event_bus.subscribe()

    # baseline batch
    rig.judge.set_batch("cfg-v1", len(tasks))
    baseline = await rig.runner.run_batch("batch-base", env, tasks, "cfg-v1", "sut-img")
    assert baseline.n == 2
    assert baseline.metrics["root_cause_accuracy"] == pytest.approx(0.4)

    # iteration: candidate cfg-v2 improves rca 0.4 -> 0.9
    rig.judge.set_batch("cfg-v2", len(tasks))
    result = await rig.engine.iterate_once(
        "sut-1",
        _profile(),
        env,
        tasks,
        "cfg-v1",
        "sut-img",
        baseline_stats=baseline,
        failed_runs=[_failed_run("run-f1", 0.4), _failed_run("run-f2", 0.4)],
        sut_config={"system_prompt.md": "always run display current-configuration"},
        hypothesis={"summary": "add missed-check guidance", "defect_class": "B"},
        candidate_config_version_id="cfg-v2",
        noise_delta=0.05,
    )

    decision: Decision = result["decision"]
    assert decision.action == "PROMOTE"
    assert decision.rule_hit == 5
    assert result["round"] == 1
    assert result["candidate_stats"].metrics["root_cause_accuracy"] == pytest.approx(0.9)
    assert result["baseline_stats"] is baseline
    assert result["hypothesis"]["status"] == "applied"
    assert result["review"]["verdict"] == "pass"
    # attribution ran and classified the causal-misattribution cluster
    clusters = result["attribution_report"].clusters
    assert clusters and clusters[0].failure_mode == "causal_misattribution"

    # EventBus: full iteration state machine observed
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    iter_events = [e for e in events if e["event"] == "iteration.status_changed"]
    states = [e["data"]["to"] for e in iter_events]
    assert states == [
        "ATTRIBUTING",
        "HYPOTHESIZING",
        "REVIEWING",
        "APPLYING",
        "VERIFYING",
        "DECIDING",
        "FINISHED",
    ]
    assert all(e["data"]["object_id"] == result["iteration_id"] for e in iter_events)

    # CheckpointStore: iteration state saved (FINISHED persisted last)
    ck = await rig.ckpt.resume(f"iter-{result['iteration_id']}")
    assert ck["state"] == "FINISHED"
    assert ck["round"] == 1

    # budget recorded the iteration scope
    usage = rig.budget.usage(result["iteration_id"])
    assert usage["tokens"] == baseline.tokens + result["candidate_stats"].tokens


@pytest.mark.asyncio
async def test_closed_loop_revert_when_candidate_regresses(tmp_path: Path) -> None:
    rig = ClosedLoopRig(tmp_path, {"cfg-v1": 0.9, "cfg-bad": 0.4})
    env, tasks = _env(), [_task(1)]

    rig.judge.set_batch("cfg-v1", 1)
    baseline = await rig.runner.run_batch("batch-base", env, tasks, "cfg-v1", "sut-img")

    rig.judge.set_batch("cfg-bad", 1)
    result = await rig.engine.iterate_once(
        "sut-1",
        _profile(),
        env,
        tasks,
        "cfg-v1",
        "sut-img",
        baseline_stats=baseline,
        candidate_config_version_id="cfg-bad",
        noise_delta=0.05,
    )
    assert result["decision"].action == "REVERT"
    assert result["decision"].rule_hit == 3
    assert rig.engine.last_state == "FINISHED"
