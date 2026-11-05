"""F9: ParallelBatchRunner integration (IN-022 slice, F6 B-3).

Two concurrent batches sharing a task-id subset must keep their checkpoints
mutually isolated: each batch's resume state lists exactly its own tasks and
run ids, and neither batch's cache/checkpoint entries bleed into the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    RootCauseExpectation,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.decision.aggregator import StatsAggregator
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.parallel import ParallelBatchRunner
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
OUT = (
    "working...\n<FINAL_JSON>"
    '{"task_complete": true, "root_cause": "GE0/0/1 shutdown"}'
    "</FINAL_JSON>\n"
)


def _env(name: str) -> EnvSpec:
    return EnvSpec(
        name=name,
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
        task_id=f"fault/par-{n:02d}",
        title=f"parallel task {n}",
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
        ),
    )


class MockJudge:
    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        from rsi.core.models import (
            Efficiency,
            PathQuality,
            SafetyVerdict,
            Scores,
            Verdict,
        )

        return Verdict(
            scores=Scores(
                root_cause_accuracy=0.9,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
            efficiency=Efficiency(tokens=100, tool_calls=1, wall_time_s=1.0),
            safety=SafetyVerdict(),
            missing=False,
        )


def _runner(tmp_path: Path, ckpt: CheckpointStore, cache: dict) -> BatchRunner:
    sandbox = SandboxService(
        backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
    )
    carrier = HostService(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
    )
    collector = TraceCollector(SeqAllocator(), None)
    TraceStore(collector=collector)
    return BatchRunner(
        sandbox=sandbox,
        carrier=carrier,
        collector=collector,
        trace_store=None,
        judge=MockJudge(),
        ckpt=ckpt,
        budget=BudgetController(
            stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
        ),
        event_bus=EventBus(),
        stats_agg=StatsAggregator(),
        result_cache=cache,
    )


class TestParallelBatchesIsolation:
    @pytest.mark.asyncio
    async def test_two_batches_shared_task_subset_no_cross_talk(
        self, tmp_path: Path
    ) -> None:
        ckpt = CheckpointStore()
        cache: dict = {}
        template = _runner(tmp_path, ckpt, cache)
        par = ParallelBatchRunner(template, max_parallel=2)

        # both batches share task 2 (same task_id) but differ in env name and
        # config version so the ADR-5 cache keys stay distinct
        specs = [
            {
                "batch_id": "batch-par-a",
                "env": _env("lab-par-a"),
                "tasks": [_task(1), _task(2)],
                "config_version_id": "cfg-a",
                "image_ref": "sut-img",
            },
            {
                "batch_id": "batch-par-b",
                "env": _env("lab-par-b"),
                "tasks": [_task(2), _task(3)],
                "config_version_id": "cfg-b",
                "image_ref": "sut-img",
            },
        ]
        stats = await par.run_many(specs)
        assert [s.n for s in stats] == [2, 2]

        # checkpoints are keyed per batch: exactly each batch's own tasks
        state_a = await ckpt.resume("batch-par-a")
        state_b = await ckpt.resume("batch-par-b")
        ids_a = {c["task_id"] for c in state_a["completed"]}
        ids_b = {c["task_id"] for c in state_b["completed"]}
        assert ids_a == {"fault/par-01", "fault/par-02"}
        assert ids_b == {"fault/par-02", "fault/par-03"}
        assert all(c["status"] == "done" for c in state_a["completed"])
        assert all(c["status"] == "done" for c in state_b["completed"])

        # the shared task executed twice (no cross-batch cache hit: distinct
        # config versions), and run ids stayed per-batch distinct
        runs_a = {c["run_id"] for c in state_a["completed"]}
        runs_b = {c["run_id"] for c in state_b["completed"]}
        assert runs_a and runs_b and not (runs_a & runs_b)

        # shared result cache served exactly 3 executed tasks (2+2 minus the
        # duplicate shared task is NOT cached across batches here because the
        # config versions differ) -> 4 executions recorded, all "done"
        assert len(state_a["completed"]) + len(state_b["completed"]) == 4

        # template runner per-batch state did not leak back (the copies run,
        # the template itself never advanced past "created")
        assert template.last_status == "created"
        assert template.last_run_ids == {}
