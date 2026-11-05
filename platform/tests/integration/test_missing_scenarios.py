"""Missing integration scenarios (H3-H6).

H3 checkpoint resume: batch crash mid-run, a fresh BatchRunner reusing the
same CheckpointStore skips already-completed tasks.
H4 budget exceeded: scope budget breach aborts the batch and publishes a
``system.alert`` ("预算超限").
H5 instrumentor chain: Instrumentor.instrument -> pg-mcp-proxy overlay ->
HostService(instrument=True) -> FakeCarrier collect.
H6 cache hit: identical (task_id, env, config_version) tasks skip SUT
execution on the second occurrence (status "cached", same verdict).

Offline only: FakeSandboxBackend + FakeCarrier + mock judge. No LLM calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.errors import BudgetExceeded
from rsi.core.models import (
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
from rsi.decision.aggregator import StatsAggregator
from rsi.instrumentor import CoverageReport, Instrumentor
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
GOOD = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown", "confidence": 0.9}'
OUT = f"working...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


def _env(name: str = "lab-h") -> EnvSpec:
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
        task_id=f"fault/hh-{n:02d}",
        title=f"H-task {n}",
        prepare=[
            PrepareAction(
                action="exec_cli",
                entries=[{"device": "R1", "cli_lines": ["interface GE0/0/1", "shutdown"]}],
            )
        ],
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
        ),
    )


def _verdict(tokens: int = 120) -> Verdict:
    return Verdict(
        scores=Scores(
            root_cause_accuracy=0.9,
            task_complete=True,
            diagnostic_steps=3,
            missed_check=0,
            false_positive=0,
        ),
        path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
        efficiency=Efficiency(tokens=tokens, tool_calls=2, wall_time_s=1.5),
        safety=SafetyVerdict(),
        missing=False,
    )


class MockJudge:
    """Scripted judge: fixed verdict; optionally raises on a given task_id."""

    def __init__(self, tokens: int = 120, crash_on: str | None = None) -> None:
        self.tokens = tokens
        self.crash_on = crash_on
        self.judged_task_ids: list[str] = []

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        task_id = getattr(getattr(task, "task", None), "task_id", None) or "?"
        if task_id == self.crash_on:
            raise RuntimeError(f"simulated crash while judging {task_id}")
        self.judged_task_ids.append(task_id)
        return _verdict(self.tokens)


def _rig(
    tmp_path: Path,
    judge: MockJudge,
    ckpt: CheckpointStore,
    budget: BudgetController,
    event_bus: EventBus,
    *,
    result_cache: dict | None = None,
) -> BatchRunner:
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
        judge=judge,
        ckpt=ckpt,
        budget=budget,
        event_bus=event_bus,
        stats_agg=StatsAggregator(),
        result_cache=result_cache,
    )


def _drain(q) -> list[dict]:
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


# ---------------------------------------------------------------- H3


class TestCheckpointResume:
    @pytest.mark.asyncio
    async def test_crash_then_resume_skips_completed(self, tmp_path: Path) -> None:
        ckpt = CheckpointStore()
        budget = BudgetController(
            stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
        )
        bus = EventBus()
        q = await bus.subscribe()

        tasks = [_task(1), _task(2), _task(3)]
        env = _env()
        crash_judge = MockJudge(crash_on=tasks[1].task_id)
        runner1 = _rig(tmp_path, crash_judge, ckpt, budget, bus)

        # run 1: task 1 completes, task 2 "crashes" mid-batch
        with pytest.raises(RuntimeError, match="simulated crash"):
            await runner1.run_batch("batch-h3", env, tasks, "cfg-v1", "sut-img")

        # checkpoint persisted only the completed task
        state = await ckpt.resume("batch-h3")
        assert state is not None
        assert [c["task_id"] for c in state["completed"]] == [tasks[0].task_id]
        assert crash_judge.judged_task_ids == [tasks[0].task_id]

        # run 2: a NEW BatchRunner sharing the checkpoint store resumes
        resume_judge = MockJudge()
        runner2 = _rig(tmp_path, resume_judge, ckpt, budget, bus)
        stats = await runner2.run_batch("batch-h3", env, tasks, "cfg-v1", "sut-img")

        # only the pending tasks executed; task 1 was skipped
        assert resume_judge.judged_task_ids == [tasks[1].task_id, tasks[2].task_id]
        assert stats.n == 2
        assert runner2.last_status == "finalized"

        state2 = await ckpt.resume("batch-h3")
        assert [c["task_id"] for c in state2["completed"]] == [
            t.task_id for t in tasks
        ]

        # the crash was surfaced on the bus only as the batch lifecycle; the
        # resumed batch reached "finalized"
        batch_events = [e for e in _drain(q) if e["event"] == "batch.status_changed"]
        assert batch_events[-1]["data"]["to"] == "finalized"


# ---------------------------------------------------------------- H4


class TestBudgetExceededStop:
    @pytest.mark.asyncio
    async def test_scope_budget_aborts_batch_and_alerts(self, tmp_path: Path) -> None:
        batch_id = "batch-h4"
        # each task reserves 50k est tokens; judge reports 60k actual tokens,
        # so task 2's reservation breaches the 100k scope limit
        budget = BudgetController(
            stop_file=tmp_path / "STOP",
            scope_limits={batch_id: 100_000},
            global_limit=10**9,
        )
        bus = EventBus()
        q = await bus.subscribe()
        judge = MockJudge(tokens=60_000)
        runner = _rig(tmp_path, judge, CheckpointStore(), budget, bus)

        tasks = [_task(1), _task(2), _task(3)]
        stats = await runner.run_batch(batch_id, _env(), tasks, "cfg-v1", "sut-img")

        # batch aborted after exactly one executed task
        assert runner.last_status == "aborted"
        assert stats.n == 1
        assert judge.judged_task_ids == [tasks[0].task_id]

        # system.alert with 预算超限 was published (critical)
        alerts = [
            e
            for e in _drain(q)
            if e["event"] == "system.alert" and "预算超限" in e["data"]["message"]
        ]
        assert alerts and alerts[0]["data"]["severity"] == "critical"
        assert alerts[0]["data"]["scope"] == batch_id

        # the scope is over budget: any further reservation raises BudgetExceeded
        with pytest.raises(BudgetExceeded):
            await budget.check_and_reserve(batch_id, 50_000)
        assert budget.usage(batch_id)["tokens"] == 60_000


# ---------------------------------------------------------------- H5


def _config_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "cfg" / "skills" / "bgp-check"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    mcp = {"mcpServers": {"netmiko": {"command": "python", "args": ["-m", "netmiko_srv"]}}}
    (tmp_path / "cfg" / "mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return tmp_path / "cfg"


class TestInstrumentorOverlayChain:
    def test_instrument_overlay_then_execute(self, tmp_path: Path) -> None:
        from rsi.core.models import TaskResult

        config_dir = _config_dir(tmp_path)
        original_mcp = (config_dir / "mcp.json").read_text(encoding="utf-8")

        def smoke(cfg: Path) -> TaskResult:
            # the instrumented side channel emits trace events
            if (cfg / "overlay.json").exists():
                sink = cfg / "trace" / "events.ndjson"
                sink.parent.mkdir(parents=True, exist_ok=True)
                sink.write_text(
                    '{"event_type": "tool_call"}\n', encoding="utf-8"
                )
            return TaskResult(task_complete=True, root_cause="GE0/0/1 shutdown")

        overlay_dir, report = Instrumentor.instrument(config_dir, "opencode", smoke)
        assert isinstance(report, CoverageReport)
        assert overlay_dir != config_dir
        assert overlay_dir.name.startswith("overlay-")

        # overlay mcp.json: netmiko wrapped with pg-mcp-proxy
        data = json.loads((overlay_dir / "mcp.json").read_text(encoding="utf-8"))
        srv = data["mcpServers"]["netmiko"]
        assert srv["command"] == "pg-mcp-proxy"
        assert srv["args"][:1] == ["--"]
        assert "python" in srv["args"]

        # original config_dir untouched
        assert (config_dir / "mcp.json").read_text(encoding="utf-8") == original_mcp
        assert not (config_dir / "plugins").exists()

        # HostService with instrument=True builds a copy-on-write overlay from
        # the exported config version and runs the SUT on it
        sandbox = SandboxService(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
        )
        carrier = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
        )
        lab = asyncio.run(sandbox.build_lab(_env()))
        instance = carrier.create_instance(
            image_ref="sut:latest",
            config_version_id=str(overlay_dir),
            lab=lab,
            permission="read-only",
            instrument=True,
            host="main",
            cred=None,
            run_id="run-h5",
        )
        assert instance.config_dir != Path(overlay_dir)
        assert instance.config_dir.name.startswith("overlay-")
        assert (instance.config_dir / "overlay.json").is_file()

        handle = carrier.submit_task(instance, TaskSpec(prompt=PROMPT))
        result = carrier.collect(handle, timeout_s=5)
        assert result.task_complete is True
        assert result.root_cause == "GE0/0/1 shutdown"

        asyncio.run(sandbox.destroy(lab))
        carrier.destroy(instance)


# ---------------------------------------------------------------- H6


class CountingHostService(HostService):
    """HostService that counts create_instance calls (cache-hit detection)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.created = 0

    def create_instance(self, *a, **kw):
        self.created += 1
        return super().create_instance(*a, **kw)


def _rig_counting(tmp_path: Path, judge, ckpt, budget, bus, cache: dict) -> tuple:
    sandbox = SandboxService(
        backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
    )
    carrier = CountingHostService(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
    )
    collector = TraceCollector(SeqAllocator(), None)
    TraceStore(collector=collector)
    runner = BatchRunner(
        sandbox=sandbox,
        carrier=carrier,
        collector=collector,
        trace_store=None,
        judge=judge,
        ckpt=ckpt,
        budget=budget,
        event_bus=bus,
        stats_agg=StatsAggregator(),
        result_cache=cache,
    )
    return runner, carrier


class TestCacheHitBatch:
    @pytest.mark.asyncio
    async def test_identical_tasks_second_is_cached(self, tmp_path: Path) -> None:
        cache: dict = {}
        bus = EventBus()
        q = await bus.subscribe()
        judge = MockJudge()
        runner, carrier = _rig_counting(
            tmp_path,
            judge,
            CheckpointStore(),
            BudgetController(
                stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
            ),
            bus,
            cache,
        )

        # two identical tasks: same task_id, same env, same config version
        tasks = [_task(1), _task(1)]
        stats = await runner.run_batch("batch-h6", _env(), tasks, "cfg-v1", "sut-img")

        # first task: full execution (instance created, judge ran once)
        assert carrier.created == 1
        assert judge.judged_task_ids == [tasks[0].task_id]

        # checkpoint: first "done", second "cached"
        state = await runner.ckpt.resume("batch-h6")
        assert len(state["completed"]) == 2
        assert state["completed"][0]["status"] == "done"
        # second entry reported as cached (same task_id, distinct run entries)
        assert state["completed"][1]["status"] == "cached"
        assert state["completed"][1]["run_id"] == state["completed"][0]["run_id"]

        # run event for the cached task was published
        run_events = [e for e in _drain(q) if e["event"] == "run.status_changed"]
        assert run_events[-1]["data"]["to"] == "cached"

        # both tasks share the same verdict -> batch aggregates both
        assert stats.n == 2
        assert stats.metrics["root_cause_accuracy"] == pytest.approx(0.9)
