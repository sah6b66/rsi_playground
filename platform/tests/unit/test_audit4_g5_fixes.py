"""Unit tests: audit round-4 group-5 fixes (D-1 / B-24 / B-23).

D-1: iterate_once exception path must leave a terminal ABORTED record
     (state + event + checkpoint) before re-raising.
B-24: L2 recovery actually restores the batch-initial snapshot (snap0) and
     replays the ops.jsonl journalled prepare ops via SandboxService.
B-23: Instrumentor.instrument runs off the event-loop thread.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from rsi.core.errors import EnvError
from rsi.core.models import (
    ExpectedAnswer,
    OptimizationProfile,
    RootCauseExpectation,
    TaskRecord,
    TaskSpec,
)
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.iteration import IterationEngine

# -- shared stubs ---------------------------------------------------------------


def _task(n: int = 1) -> TaskRecord:
    return TaskRecord(
        task_id=f"fault/g5-{n:02d}",
        title="g5",
        task=TaskSpec(prompt="p"),
        expected=ExpectedAnswer(root_cause=RootCauseExpectation(expected="x")),
    )


def _profile() -> OptimizationProfile:
    return OptimizationProfile(
        name="g5-profile",
        evalsets={"smoke": "", "iterate": "", "holdout": "es-hold"},
        goals=[{"metric": "root_cause_accuracy", "direction": "max", "weight": 1.0}],
        enabled_targets={"system_prompt"},
    )


class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str, str, str]] = []

    async def publish(self, channel, obj_id, from_, to, **kw):  # type: ignore[override]
        self.events.append((channel, obj_id, from_, to))
        await super().publish(channel, obj_id, from_, to, **kw)


class _Attr:
    def analyze(self, failed_runs, sut_config, opt_history):
        return SimpleNamespace(clusters=[])


class _Selector:
    def decide(self, ctx):
        from rsi.core.models import Decision

        return Decision(action="REVERT", rule_hit=0, reason="stub", evidence={})


class _Budget:
    async def record(self, *a, **kw):
        return None


class _Ckpt:
    def __init__(self) -> None:
        self.saved: list[tuple[str, dict[str, Any]]] = []

    async def resume(self, batch_id):
        return None

    async def save(self, key, payload):
        self.saved.append((key, dict(payload)))


class _RunnerRaising:
    """batch_runner stub that blows up mid-VERIFYING (D-1 rig)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def run_batch(self, *a, **kw):
        raise self.exc


class _RunnerStats:
    def __init__(self, stats) -> None:
        self.stats = stats

    async def run_batch(self, *a, **kw):
        return self.stats


def _stats():
    from rsi.core.models import BatchStats

    return BatchStats(
        metrics={"task_complete": 1.0, "root_cause_accuracy": 1.0},
        n=1,
        missing=0,
        tokens=10,
        tool_calls=1,
        wall_time_s=1.0,
        tokens_estimated=False,
    )


def _engine(batch_runner, bus=None, ckpt=None) -> IterationEngine:
    return IterationEngine(
        batch_runner=batch_runner,
        attribution_analyzer=_Attr(),
        selector=_Selector(),
        event_bus=bus if bus is not None else EventBus(),
        budget=_Budget(),
        ckpt=ckpt,
    )


# -- D-1: exception -> terminal ABORTED ------------------------------------------


@pytest.mark.asyncio
async def test_iterate_once_exception_leaves_aborted_terminal_state():
    boom = RuntimeError("run_batch exploded")
    bus = RecordingBus()
    ckpt = _Ckpt()
    eng = _engine(_RunnerRaising(boom), bus=bus, ckpt=ckpt)

    with pytest.raises(RuntimeError, match="run_batch exploded"):
        await eng.iterate_once(
            "sut-1", _profile(), None, [_task()], "cfg-v1", "img"
        )

    assert eng.last_state == "ABORTED"
    # an ABORTED terminal event reached the bus (from the mid-iteration state)
    assert any(e[3] == "ABORTED" for e in bus.events)
    # checkpoint carries the terminal state + the error
    ck = [
        p
        for _key, p in ckpt.saved
        if p.get("state") == "ABORTED" and p.get("error")
    ]
    assert ck and "run_batch exploded" in ck[0]["error"]
    # the engine is reusable after the abort (no zombie mid-state)
    new_id = await eng.start()
    assert new_id


@pytest.mark.asyncio
async def test_iterate_once_cancelled_also_aborts():
    class _CancelledRunner:
        async def run_batch(self, *a, **kw):
            raise asyncio.CancelledError()

    bus = RecordingBus()
    eng = _engine(_CancelledRunner(), bus=bus)

    with pytest.raises(asyncio.CancelledError):
        await eng.iterate_once("sut-1", _profile(), None, [_task()], "cfg", "img")

    assert eng.last_state == "ABORTED"
    assert any(e[3] == "ABORTED" for e in bus.events)


@pytest.mark.asyncio
async def test_iterate_once_success_unchanged():
    bus = RecordingBus()
    eng = _engine(_RunnerStats(_stats()), bus=bus)
    out = await eng.iterate_once("sut-1", _profile(), None, [_task()], "cfg", "img")
    assert eng.last_state == "FINISHED"
    assert out["decision"].action == "REVERT"


# -- B-24: real L2 snapshot recovery ----------------------------------------------


class _Snap:
    def __init__(self, sid: str) -> None:
        self.snapshot_id = sid


class _Lab:
    lab_id = "lab-g5"
    endpoints = {}


class L2Sandbox:
    """L1 (restore_task) always fails; L2 instruments are counted."""

    def __init__(self, *, l2_fails: bool = False, journal_records=None) -> None:
        self.l2_fails = l2_fails
        self.calls: list[str] = []
        self.reset_count = 0
        self.restore_count = 0
        self.replayed = None
        self.restored_applied = []
        self._journal_records = journal_records or []

    async def prepare(self, lab, task_id, actions):
        self.calls.append(f"prepare:{task_id}")
        return []

    async def restore_task(self, lab, applied):
        # L1 always fails for this rig (both the retry loop and cleanup paths)
        raise EnvError("l1 restore permanently failing")

    async def reset(self, lab):
        self.reset_count += 1
        self.calls.append("reset")

    def snapshots(self, lab_id):
        self.calls.append("snapshots")
        return [_Snap("snap0"), _Snap("snap-other")]

    async def restore(self, lab, snap):
        self.restore_count += 1
        self.calls.append(f"restore:{snap.snapshot_id}")
        if self.l2_fails:
            raise EnvError("restore failed")

    async def replay_applied_ops(self, lab, *, exclude_task_ids=None):
        self.calls.append(f"replay:{sorted(exclude_task_ids or ())}")
        self.replayed = len(self._journal_records)
        return self.replayed

    async def destroy(self, lab):
        pass


class _StubCarrier:
    def create_instance(self, **kw):
        return SimpleNamespace(instance_id="i1")

    def submit_task(self, instance, task):
        return SimpleNamespace(exit_code=None)

    def collect(self, handle, timeout_s):
        from rsi.carrier.extract import extract_task_result

        return extract_task_result(
            '{"root_cause": "x", "task_complete": true, "actions": []}'
        )

    def destroy(self, instance):
        pass


class _StubJudge:
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
                root_cause_accuracy=1.0,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", reasoning="ok"),
            efficiency=Efficiency(),
            safety=SafetyVerdict(),
            missing=False,
        )


class _StubCollector:
    def ingest(self, events):
        pass

    def finalize(self, run_id):
        pass


class _StubAgg:
    def aggregate(self, verdicts, task_ids=None):
        from rsi.core.models import BatchStats

        return BatchStats(
            metrics={"task_complete": 1.0},
            n=len(verdicts),
            tokens=0,
            tool_calls=0,
            wall_time_s=0.0,
            tokens_estimated=False,
        )


def _runner(sandbox) -> BatchRunner:
    return BatchRunner(
        sandbox=sandbox,
        carrier=_StubCarrier(),
        collector=_StubCollector(),
        trace_store=None,
        judge=_StubJudge(),
        ckpt=CheckpointStore(),
        budget=_Budget(),
        event_bus=EventBus(),
        stats_agg=_StubAgg(),
    )


@pytest.mark.asyncio
async def test_l2_recovery_restores_snap0_and_replays_journal():
    sb = L2Sandbox(
        journal_records=[{"record": "applied", "task_id": "fault/prev-01"}]
    )
    result, verdict = await _runner(sb).run_task(
        "batch-g5", _task(), _Lab(), "snap0", "cfg-v1", "img", "read-only", 0
    )
    # L1 failed 3x -> L2 ran: reset -> restore(snap0) -> journal replay
    assert sb.reset_count == 1
    assert sb.restore_count == 1
    assert "restore:snap0" in sb.calls  # the batch-initial snapshot, not a reset-only
    assert sb.replayed == 1
    assert "replay:['fault/g5-01']" in sb.calls  # current task excluded
    assert not verdict.missing  # L2 success -> task scores normally


@pytest.mark.asyncio
async def test_l2_failure_env_failed():
    sb = L2Sandbox(l2_fails=True)
    result, verdict = await _runner(sb).run_task(
        "batch-g5", _task(), _Lab(), "snap0", "cfg-v1", "img", "read-only", 0
    )
    assert verdict.missing  # L2 also failed -> env_failed, 0 score, no raise
    assert sb.restore_count == 1


# -- B-23: instrument off the event loop -------------------------------------------


class ThreadRecordingInstrumentor:
    is_static = True

    def __init__(self) -> None:
        self.threads: list[int] = []

    def instrument(self, config_dir, carrier, smoke_fn):
        self.threads.append(threading.get_ident())
        # the smoke callback must be invokable from the worker thread
        smoke_fn(config_dir)
        from rsi.instrumentor.report import CoverageReport

        return config_dir, CoverageReport()

    def verify(self, *a, **kw):
        return None


@pytest.mark.asyncio
async def test_instrument_runs_off_loop_thread():
    main_thread = threading.get_ident()
    sb = L2Sandbox()
    inst = ThreadRecordingInstrumentor()

    runner = _runner(sb)
    runner.instrumentor = inst
    # run_task needs a real config dir to reach the instrument call
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "cfg"
        cfg.mkdir()
        await runner.run_task(
            "batch-g5", _task(), _Lab(), "snap0", str(cfg), "img", "read-only", 0
        )
    assert inst.threads, "instrument must have been called"
    assert all(t != main_thread for t in inst.threads)
