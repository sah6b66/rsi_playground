"""D-4 ADR-5 content-hash cache + bypass_cache, D-5 check_global wiring,
D-11 det_timeout / checkpoint throttle (F4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.models import (
    DeviceSpec,
    Efficiency,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    PathQuality,
    PrepareAction,
    RootCauseExpectation,
    Scores,
    SshAccess,
    SafetyVerdict,
    TaskRecord,
    TaskSpec,
    Topology,
    Verdict,
)
from rsi.decision.aggregator import StatsAggregator
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator

pytestmark = pytest.mark.asyncio

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
GOOD = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown", "confidence": 0.9}'
OUT = f"working...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


def _env() -> EnvSpec:
    return EnvSpec(
        name="lab-a5",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R1:GE0/0/2"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="a", password="p")
        },
    )


def _task(prompt: str = PROMPT, n: int = 1) -> TaskRecord:
    return TaskRecord(
        task_id=f"fault/hh-{n:02d}",
        title="t",
        prepare=[
            PrepareAction(
                action="exec_cli",
                entries=[{"device": "R1", "cli_lines": ["shutdown"]}],
            )
        ],
        task=TaskSpec(prompt=prompt),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
        ),
    )


class MockJudge:
    def __init__(self) -> None:
        self.calls = 0

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        self.calls += 1
        return Verdict(
            scores=Scores(
                root_cause_accuracy=0.9,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", skipped_critical_steps=[],
                                     reasoning=""),
            efficiency=Efficiency(tokens=100, tool_calls=1, wall_time_s=0.1),
            safety=SafetyVerdict(),
            missing=False,
        )


class CountingCarrier(HostService):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.created = 0

    def create_instance(self, *a, **kw):
        self.created += 1
        return super().create_instance(*a, **kw)


def _rig(tmp_path: Path, judge, ckpt, budget, cache, bus=None) -> tuple:
    sandbox = SandboxService(
        backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
    )
    carrier = CountingCarrier(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
    )
    collector = TraceCollector(SeqAllocator(), None)
    runner = BatchRunner(
        sandbox=sandbox,
        carrier=carrier,
        collector=collector,
        trace_store=None,
        judge=judge,
        ckpt=ckpt,
        budget=budget,
        event_bus=bus or EventBus(),
        stats_agg=StatsAggregator(),
        result_cache=cache,
    )
    return runner, carrier


def _budget(tmp_path: Path, global_limit: int = 10**9) -> BudgetController:
    return BudgetController(
        stop_file=tmp_path / "STOP", scope_limits={}, global_limit=global_limit
    )


class TestCacheKeyAdr5:
    async def test_same_task_rerun_hits_cache(self, tmp_path):
        judge = MockJudge()
        cache: dict = {}
        runner, carrier = _rig(
            tmp_path, judge, CheckpointStore(), _budget(tmp_path), cache
        )
        await runner.run_batch("b1", _env(), [_task()], "cfg-v1", "sut")
        n_before = carrier.created
        await runner.run_batch("b2", _env(), [_task()], "cfg-v1", "sut")
        assert carrier.created == n_before  # no second execution
        assert len(cache) == 1

    async def test_prompt_change_misses_cache(self, tmp_path):
        judge = MockJudge()
        cache: dict = {}
        runner, carrier = _rig(
            tmp_path, judge, CheckpointStore(), _budget(tmp_path), cache
        )
        await runner.run_batch("b1", _env(), [_task(prompt=PROMPT)], "cfg-v1", "sut")
        n_before = carrier.created
        # one character changed in the prompt -> new content hash -> miss
        await runner.run_batch(
            "b2", _env(), [_task(prompt=PROMPT + "!")], "cfg-v1", "sut"
        )
        assert carrier.created == n_before + 1
        assert len(cache) == 2

    async def test_env_content_change_misses_cache(self, tmp_path):
        judge = MockJudge()
        cache: dict = {}
        runner, _ = _rig(tmp_path, judge, CheckpointStore(), _budget(tmp_path), cache)
        env2 = _env().model_copy(deep=True)
        env2 = env2.model_copy(
            update={
                "devices": [
                    env2.devices[0].model_copy(
                        update={"config": "configs/R1-alt.cfg"}
                    )
                ]
            }
        )
        await runner.run_batch("b1", _env(), [_task()], "cfg-v1", "sut")
        await runner.run_batch("b2", env2, [_task()], "cfg-v1", "sut")
        assert judge.calls == 2  # both executed

    async def test_config_version_change_does_not_reuse(self, tmp_path):
        judge = MockJudge()
        cache: dict = {}
        runner, _ = _rig(tmp_path, judge, CheckpointStore(), _budget(tmp_path), cache)
        await runner.run_batch("b1", _env(), [_task()], "cfg-v1", "sut")
        await runner.run_batch("b2", _env(), [_task()], "cfg-v2", "sut")
        assert judge.calls == 2  # same content, different version -> no reuse

    async def test_bypass_cache_skips_read_and_write(self, tmp_path):
        judge = MockJudge()
        cache: dict = {}
        runner, carrier = _rig(
            tmp_path, judge, CheckpointStore(), _budget(tmp_path), cache
        )
        await runner.run_batch("b1", _env(), [_task()], "cfg-v1", "sut")
        assert len(cache) == 1
        n_before = carrier.created
        await runner.run_batch(
            "b2", _env(), [_task()], "cfg-v1", "sut", bypass_cache=True
        )
        assert carrier.created == n_before + 1  # executed again
        assert len(cache) == 1  # nothing new written through bypass


class TestGlobalStop:
    async def test_global_budget_breach_aborts_batch_writes_stop(self, tmp_path):
        judge = MockJudge()
        budget = _budget(tmp_path, global_limit=50)  # one task uses 100 tokens
        runner, carrier = _rig(
            tmp_path, judge, CheckpointStore(), budget, {}
        )
        stats = await runner.run_batch(
            "b1", _env(), [_task(n=1), _task(n=2)], "cfg-v1", "sut"
        )
        assert runner.last_status == "aborted"
        assert budget.check_stop_file() is True  # STOP file exists
        assert judge.calls == 1  # first task ran, breaker aborted before #2


class TestDetTimeout:
    async def test_hanging_exec_readonly_times_out(self, tmp_path):
        from rsi.judge.deterministic import DeterministicJudge
        from rsi.core.models import EvalTask, LabHandle, TaskResult, StateAssert

        task = _task().model_copy(
            update={
                "expected": _task().expected.model_copy(
                    update={
                        "final_state": [
                            StateAssert(
                                device="R1",
                                cli="display version",
                                expect_contains=["Version"],
                            )
                        ]
                    }
                )
            }
        )
        lab = LabHandle(lab_id="l", host="main", backend="fake", endpoints={})

        class HangingSandbox:
            def exec_readonly(self, lab, device, cli):
                # simulate a wedged device session (blocks forever)
                import threading
                e = threading.Event()
                e.wait(timeout=30)
                return ""

        det = DeterministicJudge(timeout_s=0.2)
        scores = await det.evaluate(
            EvalTask(env=_env(), task=task),
            TaskResult(task_complete=True),
            lab,
            HangingSandbox(),  # type: ignore[arg-type]
            events=[],
        )
        assert scores.final_state_results[0].passed is False  # not hung


class TestCheckpointThrottle:
    async def test_high_frequency_saves_merge(self):
        store = CheckpointStore(interval_s=3600)  # effectively one flush
        for i in range(10):
            await store.save("scope", {"i": i})
        # only the first save committed; the rest stayed pending
        assert store.flush_count < 10
        state = await store.resume("scope")
        assert state == {"i": 9}  # freshest state visible on resume

    async def test_forced_save_commits(self):
        store = CheckpointStore(interval_s=3600)
        await store.save("s", {"i": 1})
        await store.save("s", {"i": 2}, force=True)
        assert store.flush_count == 2

    async def test_default_store_unthrottled(self):
        store = CheckpointStore()
        for i in range(5):
            await store.save("s", {"i": i})
        assert store.flush_count == 5
