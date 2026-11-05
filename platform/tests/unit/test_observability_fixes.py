"""Unit tests for observability fixes H1/H2/H7/H8.

H1: settings wiring (BatchRunner / SandboxService / HostService)
H2: system.alert publishers (circuit breaker / STOP / budget / smoke / global)
H7: trace collector NDJSON persistence
H8: token estimation fallback (estimate_tokens + wiretap usage fill)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.event_bus import EventBus
from rsi.settings import Settings
from rsi.tracing.collector import TraceCollector
from rsi.tracing.parsers import OpenCodeWiretapParser, estimate_tokens
from rsi.tracing.seq import SeqAllocator


# ---------------------------------------------------------------------------
# H1: settings wiring
# ---------------------------------------------------------------------------


class _Null:
    def __getattr__(self, name):
        return _Null()

    def __call__(self, *a, **kw):  # pragma: no cover
        raise TypeError("null collaborator called")


def _runner(settings=None, task_timeout_s=None) -> BatchRunner:
    return BatchRunner(
        sandbox=_Null(),
        carrier=_Null(),
        collector=_Null(),
        trace_store=_Null(),
        judge=_Null(),
        ckpt=_Null(),
        budget=_Null(),
        event_bus=_Null(),
        stats_agg=_Null(),
        settings=settings,
        task_timeout_s=task_timeout_s,
    )


class TestSettingsWiring:
    def test_default_timeout_without_settings(self):
        assert _runner().task_timeout_s == 1800.0

    def test_settings_provide_task_timeout(self):
        settings = Settings()
        settings.carrier.task_timeout_s = 777
        assert _runner(settings=settings).task_timeout_s == 777.0

    def test_explicit_timeout_wins_over_settings(self):
        settings = Settings()
        settings.carrier.task_timeout_s = 777
        runner = _runner(settings=settings, task_timeout_s=99.0)
        assert runner.task_timeout_s == 99.0

    def test_settings_provide_build_retry_and_ready_timeout(self):
        settings = Settings()
        settings.sandbox.build_retry = 4
        settings.sandbox.ready_timeout_s = 123
        runner = _runner(settings=settings)
        assert runner.prepare_attempts == 5  # 1 initial + 4 retries
        assert runner.ready_timeout_s == 123

    def test_defaults_without_settings(self):
        runner = _runner()
        assert runner.prepare_attempts == 3
        assert runner.ready_timeout_s == 600

    def test_sandbox_service_reads_settings(self, tmp_path: Path):
        from rsi.sandbox.backends.fake import FakeSandboxBackend
        from rsi.sandbox.service import SandboxService

        settings = Settings()
        settings.sandbox.build_retry = 5
        settings.sandbox.ready_timeout_s = 42
        svc = SandboxService(
            backends={"fake": FakeSandboxBackend()},
            labs_dir=tmp_path,
            settings=settings,
        )
        assert svc._build_retry == 5
        assert svc._ready_timeout_s == 42

    def test_host_service_scheduler_reads_max_instances(self):
        from rsi.carrier.adapters.fake import FakeCarrier
        from rsi.carrier.hosts import HostPool
        from rsi.carrier.network import NetPolicy
        from rsi.carrier.service import HostService

        settings = Settings()
        settings.carrier.max_instances_per_host = 3
        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier({})},
            settings=settings,
        )
        assert svc.scheduler.default_max == 3


# ---------------------------------------------------------------------------
# H2: system.alert publishers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAlertPublishers:
    async def _collect(self, q) -> list[dict]:
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        return msgs

    async def test_circuit_breaker_publishes_critical_alert(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _env, _task, _verdict

        rig = Rig(tmp_path)
        rig.judge.verdict = _verdict(missing=True)
        q = await rig.event_bus.subscribe()
        tasks = [_task(i) for i in range(1, 4)]
        await rig.runner.run_batch("batch-1", _env(), tasks, "cfg-v1", "sut-img")
        alerts = [m for m in await self._collect(q) if m["event"] == "system.alert"]
        assert alerts, "expected a system.alert on circuit breaker"
        assert alerts[0]["data"]["severity"] == "critical"
        assert "连续 3 个任务环境失败" in alerts[0]["data"]["message"]
        assert alerts[0]["data"]["scope"] == "batch-1"

    async def test_stop_file_publishes_warning_alert(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path)
        rig.budget.stop_file.write_text("stop", encoding="utf-8")
        q = await rig.event_bus.subscribe()
        await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        alerts = [m for m in await self._collect(q) if m["event"] == "system.alert"]
        assert alerts
        assert alerts[0]["data"]["severity"] == "warning"
        assert "STOP" in alerts[0]["data"]["message"]

    async def test_budget_exceeded_publishes_critical_alert(self, tmp_path):
        from tests.unit.test_batch_runner import Rig, _env, _task

        rig = Rig(tmp_path, scope_limit=10)
        q = await rig.event_bus.subscribe()
        await rig.runner.run_batch("batch-1", _env(), [_task()], "cfg-v1", "sut-img")
        alerts = [m for m in await self._collect(q) if m["event"] == "system.alert"]
        assert alerts
        assert alerts[0]["data"]["severity"] == "critical"
        assert "预算超限" in alerts[0]["data"]["message"]

    async def test_global_budget_publishes_alert(self, tmp_path):
        bus = EventBus()
        q = await bus.subscribe()
        budget = BudgetController(
            stop_file=tmp_path / "STOP",
            scope_limits={},
            global_limit=100,
            event_bus=bus,
        )
        await budget.record("s", 200, 0.0, 1.0)
        from rsi.core.errors import BudgetExceeded

        with pytest.raises(BudgetExceeded):
            await budget.check_global()
        alerts = [m for m in await self._collect(q) if m["event"] == "system.alert"]
        assert alerts
        assert alerts[0]["data"]["severity"] == "critical"
        assert "全局预算超限" in alerts[0]["data"]["message"]

    async def test_smoke_short_circuit_publishes_info_alert(self):
        from rsi.core.models import BatchStats, OptimizationProfile

        from rsi.orchestrator.iteration import IterationEngine
        from tests.unit.test_iteration_gates import (
            StubAttribution,
            StubBudget,
            StubRunner,
            _profile,
            _stats,
        )

        class Sel:
            def decide(self, ctx):
                from rsi.core.models import Decision

                return Decision(action="REVERT", rule_hit=0, reason="t", evidence={})

        bus = EventBus()
        q = await bus.subscribe()
        engine = IterationEngine(
            batch_runner=StubRunner({"verify-smoke": _stats(0.0)}),
            attribution_analyzer=StubAttribution(),
            selector=Sel(),
            event_bus=bus,
            budget=StubBudget(),
        )
        result = await engine.iterate_once(
            "sut-1", _profile(), None, [], "cfg-v1", "img"
        )
        assert result["short_circuit"] == "smoke"
        alerts = [m for m in await self._collect(q) if m["event"] == "system.alert"]
        assert alerts
        assert alerts[0]["data"]["severity"] == "info"
        assert "冒烟集未通过" in alerts[0]["data"]["message"]
        assert alerts[0]["data"]["scope"] == engine.last_iteration_id


# ---------------------------------------------------------------------------
# H7: collector persistence
# ---------------------------------------------------------------------------


def _ev(run_id: str, seq: int = 0, ts: str = "2026-11-02T10:00:00.000Z") -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        task_id="t1",
        lab_id="lab1",
        sut_id="sut-1",
        seq=seq,
        ts=ts,
        event_type=EventType.TOOL_CALL,
        data={"cmd": "display clock"},
    )


class TestCollectorPersistence:
    def test_ndjson_and_meta_written(self, tmp_path: Path):
        col = TraceCollector(SeqAllocator(), None, persistence_dir=tmp_path)
        col.ingest([_ev("run-a", ts="2026-11-02T10:00:01.000Z")])
        col.ingest([_ev("run-a", ts="2026-11-02T10:00:02.000Z")])
        col.finalize("run-a")

        ndjson = tmp_path / "run-a" / "events.ndjson"
        assert ndjson.is_file()
        lines = [json.loads(x) for x in ndjson.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(lines) == 2
        assert [l["seq"] for l in lines] == [0, 1]
        assert lines[0]["run_id"] == "run-a"

        meta = json.loads((tmp_path / "run-a" / "meta.json").read_text(encoding="utf-8"))
        assert meta["event_count"] == 2
        assert meta["first_ts"] == "2026-11-02T10:00:01.000Z"
        assert meta["last_ts"] == "2026-11-02T10:00:02.000Z"

    def test_no_persistence_keeps_memory_only(self, tmp_path: Path):
        col = TraceCollector(SeqAllocator(), None)
        col.ingest([_ev("run-b")])
        col.finalize("run-b")
        assert not (tmp_path / "run-b").exists()
        assert col.get_meta("run-b")["event_count"] == 1


# ---------------------------------------------------------------------------
# H8: token estimation
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    def test_empty_text(self):
        assert estimate_tokens("") == (0, False)

    def test_estimates_chars_over_four(self):
        assert estimate_tokens("x" * 40) == (10, True)
        assert estimate_tokens("abcdefgh") == (2, True)

    def test_short_text_still_estimated(self):
        assert estimate_tokens("ab") == (0, True)

    def test_wiretap_fills_estimated_usage(self):
        parser = OpenCodeWiretapParser()
        frame = json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "2026-11-02T10:00:00.000Z",
                "model": "m1",
                "system": [{"text": "You are a network SRE."}],
                "messages": [{"role": "user", "content": "ping fails"}],
                "tools": [],
            }
        )
        ev = parser.parse_line(frame, "run-1", "t1", "lab1", "sut1")
        assert ev is not None
        usage = ev.data["usage"]
        assert usage["estimated"] is True
        text = "You are a network SRE." + "\n" + "ping fails"
        assert usage["input_tokens"] == len(text) // 4

    def test_wiretap_keeps_real_usage(self):
        parser = OpenCodeWiretapParser()
        frame = json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "2026-11-02T10:00:00.000Z",
                "system": [{"text": "sys"}],
                "messages": [],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            }
        )
        ev = parser.parse_line(frame, "run-1", "t1", "lab1", "sut1")
        assert ev is not None
        assert "usage" not in ev.data  # real usage preserved, no estimate
