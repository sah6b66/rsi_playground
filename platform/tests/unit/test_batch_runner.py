"""Unit tests: BatchRunner 9-step pipeline (DD-12 §5.1/§5.2).

Real SandboxService (FakeSandboxBackend) + real HostService (FakeCarrier) +
mock JudgeService returning pre-set Verdicts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.models import (
    CliEntry,
    DeviceSpec,
    Efficiency,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
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
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.decision.aggregator import StatsAggregator
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore

PROMPT = "diagnose the bgp session flap and reply with FINAL_JSON"
GOOD = '{"task_complete": true, "root_cause": "bfd timer mismatch", "confidence": 0.9}'
OUT = f"agent output...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


def _env() -> EnvSpec:
    return EnvSpec(
        name="lab-a",
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


def _task(n: int = 1, prepare_ok: bool = True) -> TaskRecord:
    lines = (
        ["interface GE0/0/1", "shutdown"] if prepare_ok else ["display frobnicate"]
    )
    return TaskRecord(
        task_id=f"fault/demo-{n:02d}",
        title=f"demo task {n}",
        prepare=[PrepareAction(action="exec_cli", entries=[CliEntry(device="R1", cli_lines=lines)])],
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(root_cause=RootCauseExpectation(expected="bfd timer mismatch")),
    )


def _verdict(missing: bool = False, rca: float = 0.8) -> Verdict:
    return Verdict(
        scores=Scores(
            root_cause_accuracy=0.0 if missing else rca,
            task_complete=not missing,
            diagnostic_steps=0 if missing else 3,
            missed_check=0,
            false_positive=0,
        ),
        path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
        efficiency=Efficiency(tokens=120, tool_calls=2, wall_time_s=1.5),
        safety=SafetyVerdict(),
        missing=missing,
    )


class FakeJudge:
    """Mock JudgeService: records call order, returns a pre-set Verdict."""

    def __init__(self, verdict: Verdict | None = None) -> None:
        self.verdict = verdict or _verdict()
        self.calls: list[dict] = []
        self.order: list[str] | None = None  # shared order log with test

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        self.calls.append(
            {
                "task_id": task.task.task_id,
                "result": result,
                "events": list(events),
                "lab_id": lab.lab_id,
                "permission": permission,
            }
        )
        if self.order is not None:
            self.order.append("judge")
        return self.verdict.model_copy()


class Rig:
    def __init__(self, tmp_path: Path, responses: dict[str, str] | None = None,
                 scope_limit: int = 10_000_000) -> None:
        self.backend = FakeSandboxBackend()
        self.sandbox = SandboxService(backends={"fake": self.backend}, labs_dir=tmp_path)
        self.carrier = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses or {PROMPT: OUT})},
        )
        self.judge = FakeJudge()
        self.collector = TraceCollector(SeqAllocator(), None)
        self.trace_store = TraceStore(self.collector)
        self.ckpt = CheckpointStore()
        self.budget = BudgetController(
            stop_file=tmp_path / "STOP",
            scope_limits={"batch-1": scope_limit},
            global_limit=10**9,
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
        self.events_q = None


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


@pytest.mark.asyncio
class TestRunBatch:
    async def test_single_task_happy_path(self, tmp_path):
        rig = Rig(tmp_path)
        task = _task()
        stats = await rig.runner.run_batch(
            "batch-1", _env(), [task], "cfg-v1", "sut-img", "read-only"
        )
        assert stats.n == 1
        assert stats.missing == 0
        assert stats.metrics["root_cause_accuracy"] == 0.8
        # judge saw the collected TaskResult
        assert len(rig.judge.calls) == 1
        assert rig.judge.calls[0]["result"].task_complete is True
        assert rig.judge.calls[0]["result"].root_cause == "bfd timer mismatch"
        # trace events ingested and finalized
        meta = rig.collector.get_meta(rig.runner.last_run_ids[task.task_id])
        assert meta is not None and meta["finalized"] is True
        # instance destroyed
        assert rig.carrier._instances == {}
        # lab destroyed
        assert rig.backend._labs == {}

    async def test_restore_after_judge_and_config_restored(self, tmp_path):
        rig = Rig(tmp_path)
        order: list[str] = []
        rig.judge.order = order
        orig_restore = rig.sandbox.restore_task

        async def restore_rec(lab, applied):
            order.append("restore")
            return await orig_restore(lab, applied)

        rig.sandbox.restore_task = restore_rec
        await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        assert order == ["judge", "restore"]

    async def test_prepare_failure_env_failed_continue(self, tmp_path):
        rig = Rig(tmp_path)
        tasks = [_task(1, prepare_ok=False), _task(2, prepare_ok=True)]
        stats = await rig.runner.run_batch("batch-1", _env(), tasks, "cfg-v1", "sut-img")
        assert stats.n == 2
        assert stats.missing == 1
        # env-failed run scores 0 but stays in the denominator
        assert stats.metrics["root_cause_accuracy"] == pytest.approx(0.8 / 2)
        # second task still judged
        assert len(rig.judge.calls) == 1
        # checkpoint records both completions
        state = await rig.ckpt.resume("batch-1")
        assert [c["status"] for c in state["completed"]] == ["env_failed", "done"]

    async def test_multiple_tasks_sequential(self, tmp_path):
        rig = Rig(tmp_path)
        tasks = [_task(1), _task(2), _task(3)]
        stats = await rig.runner.run_batch("batch-1", _env(), tasks, "cfg-v1", "sut-img")
        assert stats.n == 3
        assert len(rig.judge.calls) == 3
        state = await rig.ckpt.resume("batch-1")
        assert len(state["completed"]) == 3
        assert [c["task_id"] for c in state["completed"]] == [t.task_id for t in tasks]
        assert len(set(rig.runner.last_run_ids.values())) == 3

    async def test_stop_file_aborts_batch(self, tmp_path):
        rig = Rig(tmp_path)
        rig.budget.stop_file.write_text("stop", encoding="utf-8")
        stats = await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        assert stats.n == 0
        assert rig.runner.last_status == "aborted"
        assert rig.judge.calls == []
        assert rig.backend._labs == {}  # lab still destroyed

    async def test_budget_exceeded_aborts_batch(self, tmp_path):
        rig = Rig(tmp_path, scope_limit=10)  # any reservation breaches
        stats = await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        assert stats.n == 0
        assert rig.runner.last_status == "aborted"
        assert rig.judge.calls == []
        assert rig.backend._labs == {}

    async def test_events_published(self, tmp_path):
        rig = Rig(tmp_path)
        q = await rig.event_bus.subscribe()
        await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        names = [e["event"] for e in events]
        assert "run.status_changed" in names
        assert "batch.status_changed" in names
