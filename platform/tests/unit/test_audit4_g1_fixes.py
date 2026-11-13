"""Audit round-4 group-1 fixes (see .audit4/T1-b.md and T2.md).

Covers:
- B-21: run_batch exception path still writes the terminal batches row
  (status failed + finished_at) before re-raising
- B-22: instrumentor.instrument / carrier.create_instance failures degrade
  to task-level env handling instead of killing the batch
- W-A: lifespan shutdown cancels in-flight batches (DB aborted, no hanging
  tasks) and disposes the DB engine
- W-B: per-task progress callbacks surface running-state on_update
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session

from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.orchestrator.batch import BatchRunner

pytestmark = pytest.mark.asyncio


def _make_db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path}/audit4.db")
    SQLModel.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, class_=Session)


def _wire_db(runner: BatchRunner, factory) -> None:
    runner.session_factory = factory


# ── B-21: exception path terminal state ──────────────────────────────────


class TestB21ExceptionTerminalState:
    async def test_judge_crash_leaves_failed_db_row(self, tmp_path):
        from rsi.db.tables import Batch
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        engine, factory = _make_db(tmp_path)
        _wire_db(rig.runner, factory)

        async def boom(*a, **kw):
            raise RuntimeError("judge exploded")

        rig.judge.judge_run = boom  # type: ignore
        with pytest.raises(RuntimeError):
            await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        # memory + DB both terminal
        assert rig.runner.last_status == "failed"
        with factory() as s:
            row = s.get(Batch, "batch-1")
        assert row is not None, "batches row must exist"
        assert row.status == "failed"
        assert row.finished_at

    async def test_cancellation_leaves_aborted_db_row(self, tmp_path):
        from rsi.db.tables import Batch
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        engine, factory = _make_db(tmp_path)
        _wire_db(rig.runner, factory)

        # slow prepare so cancellation lands mid-task
        orig_prepare = rig.sandbox.prepare

        async def slow_prepare(lab, task_id, actions):
            await asyncio.sleep(10)
            return await orig_prepare(lab, task_id, actions)

        rig.sandbox.prepare = slow_prepare  # type: ignore
        coro = rig.runner.run_batch(
            "batch-1", _env(), [_task()], "cfg-v1", "sut-img"
        )
        task = asyncio.get_running_loop().create_task(coro)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rig.runner.last_status == "aborted"
        with factory() as s:
            row = s.get(Batch, "batch-1")
        assert row is not None
        assert row.status == "aborted"
        assert row.finished_at


# ── B-22: task-level instance failures ───────────────────────────────────


class _ExplodingInstrumentor:
    def instrument(self, config_dir, carrier_type, smoke_fn):
        raise ValueError("copytree nested dir")

    def verify(self, *a, **kw):  # pragma: no cover - not reached
        return True


class TestB22TaskLevelFailures:
    async def test_instrument_crash_degrades_to_original_config(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        config_dir = tmp_path / "cfg-export"
        config_dir.mkdir()
        rig.runner.instrumentor = _ExplodingInstrumentor()

        seen_configs: list[str] = []
        orig_create = rig.carrier.create_instance

        def create_rec(**kw):
            seen_configs.append(str(kw.get("config_version_id")))
            return orig_create(**kw)

        rig.carrier.create_instance = create_rec  # type: ignore
        stats = await rig.runner.run_batch(
            "batch-1", _env(), [_task()], str(config_dir), "sut-img"
        )
        assert stats.n == 1 and stats.missing == 0  # task executed normally
        assert len(rig.judge.calls) == 1
        assert seen_configs == [str(config_dir)]  # fell back to original dir

    async def test_create_instance_crash_scores_env_failed_batch_alive(self, tmp_path):
        from rsi.core.errors import CarrierError
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        calls = {"n": 0}
        orig_create = rig.carrier.create_instance

        def create_flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CarrierError("net attach failed")
            return orig_create(**kw)

        rig.carrier.create_instance = create_flaky  # type: ignore
        tasks = [_task(1), _task(2)]
        stats = await rig.runner.run_batch("batch-1", _env(), tasks, "cfg-v1", "sut-img")
        assert stats.n == 2
        assert stats.missing == 1  # first task env_failed (missing=1)
        assert len(rig.judge.calls) == 1  # second task judged normally
        assert rig.runner.last_status == "finalized"  # batch survived


# ── W-A: shutdown cancels in-flight batches ──────────────────────────────


class _SlowSandboxForShutdown:
    """Fake sandbox: fast build, prepare blocks 10s (cancellation target)."""

    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def build_lab(self, env):
        return type("Lab", (), {"lab_id": "lab-1", "endpoints": {}})()

    async def wait_ready(self, lab, timeout):
        return None

    async def snapshot(self, lab, name="snap0"):
        return type("Snap", (), {"snapshot_id": "s0"})()

    def snapshots(self, lab_id):
        # F6/R5：BatchRunner 新建路径经 build_lab 的 snap0 登记（snapshots
        # 列表）取基线，不再显式补拍。
        return [type("Snap", (), {"snapshot_id": "s0"})()]

    async def prepare(self, lab, task_id, actions):
        await asyncio.sleep(10)
        return []

    async def restore_task(self, lab, applied):
        return None

    async def destroy(self, lab):
        self.destroyed.append(lab.lab_id)


class _NullBudget:
    def check_stop_file(self):
        return False

    async def check_and_reserve(self, *a, **kw):
        return None

    async def record(self, *a, **kw):
        return None

    async def check_global(self):
        return None


class _NullBus:
    async def publish(self, *a, **kw):
        return None


class _NullCkpt:
    async def resume(self, batch_id):
        return None

    async def save(self, *a, **kw):
        return None


class _NullStatsAgg:
    def aggregate(self, verdicts, task_ids=None):
        from types import SimpleNamespace

        return SimpleNamespace(n=0, missing=0)


class TestWAShutdown:
    async def test_lifespan_shutdown_cancels_batches_and_disposes_engine(self, tmp_path):
        from rsi.db.tables import Batch
        from rsi.api.app import create_app

        engine, factory = _make_db(tmp_path)
        disposed = {"n": 0}

        class _EngineProxy:
            def dispose(self):
                disposed["n"] += 1

        sandbox = _SlowSandboxForShutdown()
        runner = BatchRunner(
            sandbox=sandbox,
            carrier=None,
            collector=None,
            trace_store=None,
            judge=None,
            ckpt=_NullCkpt(),
            budget=_NullBudget(),
            event_bus=_NullBus(),
            stats_agg=_NullStatsAgg(),
            session_factory=factory,
        )

        from rsi.core.models import ExpectedAnswer

        task_rec = TaskRecord(
            task_id="fault/w-a-00",
            title="t",
            prepare=[],
            task=TaskSpec(prompt="p"),
            expected=ExpectedAnswer(),
        )
        env = EnvSpec(
            name="lab-wa",
            backend="fake",
            devices=[DeviceSpec(name="R1", type="vrp", config="c")],
            topology=Topology(),
            ssh_access={"R1": SshAccess(ip="10.0.0.1", port=1, username="a", password="p")},
        )
        runner.batch_resolver = lambda batch: (env, [task_rec], "cfg-v1", "sut")

        services = {
            "batch_runner": runner,
            "engine": _EngineProxy(),
            "sandbox_service": None,
        }
        app = create_app(services)
        updates: list[dict] = []
        # httpx ASGITransport does not run the ASGI lifespan; drive it
        # directly so the shutdown path actually executes on context exit.
        async with app.router.lifespan_context(app):
            copy = runner.trigger({"id": "batch-wa"}, on_update=updates.append)
            await asyncio.sleep(0.1)  # let it enter prepare (the 10s sleep)
            assert copy.last_status == "running"
        # lifespan shutdown ran on context exit
        statuses = [u.get("status") for u in updates]
        assert "aborted" in statuses, updates
        assert not runner._tasks, "no hanging batch tasks after shutdown"
        assert disposed["n"] == 1, "engine disposed exactly once"
        with factory() as s:
            row = s.get(Batch, "batch-wa")
        assert row is not None and row.status == "aborted" and row.finished_at
        assert sandbox.destroyed, "lab destroyed on cancellation path"


# ── W-B: running-state progress callbacks ────────────────────────────────


class TestWBProgress:
    async def test_two_task_batch_emits_running_progress(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        updates: list[dict] = []

        def on_update(upd: dict) -> None:
            updates.append(upd)

        tasks = [_task(1), _task(2)]
        env = _env()

        def resolver(batch):
            return env, tasks, "cfg-v1", "sut"

        rig.runner.batch_resolver = resolver
        copy = rig.runner.trigger({"id": "batch-wb"}, on_update=on_update)
        await asyncio.wait_for(
            asyncio.gather(*copy._tasks, return_exceptions=True), timeout=30
        )
        assert copy.last_status == "finalized"
        assert len(updates) >= 3, updates  # >=2 running + 1 finalized
        assert updates[-1]["status"] == "finalized"
        running = [u for u in updates[:-1] if u["status"] == "running"]
        assert running, updates
        dones = [u["progress"]["done"] for u in running]
        assert dones == sorted(dones) and dones[0] >= 1, dones  # monotonic
        assert running[-1]["progress"] == {"done": 2, "total": 2}
