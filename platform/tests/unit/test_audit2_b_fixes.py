"""Audit2 B-group fixes (B-1..B-18): execution layer wiring/contract tests.

Covers: instrumentor wiring into BatchRunner (B-1), labs /
instrumentation_reports DB writes (B-2), max_parallel_batches consumer
(B-3), SshPool alive probing (B-4), verify_image (B-5), vendor reset_cmd +
equivalence diff (B-6), health timestamp judgement (B-7), create_instance
cleanup (B-9), RunHandle.exit_code (B-10), bootstrap event type (B-12),
ReadinessChecker fallback (B-15), carrier-type adapter dispatch (B-18).
B-11 (pg-mcp-proxy) lives in tests/integration/test_pg_mcp_proxy.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi.carrier.adapters.fake import FakeCarrier, RunHandle
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.models import (
    CliEntry,
    DeviceSpec,
    EnvSpec,
    LinkSpec,
    PrepareAction,
    SshAccess,
    TaskSpec,
    Topology,
)
from rsi.decision.aggregator import StatsAggregator
from rsi.instrumentor.report import CoverageReport, CoveredItem, NotInstrumentable
from rsi.orchestrator.batch import BatchRunner
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.parallel import ParallelBatchRunner
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.readiness import ReadyReport
from rsi.sandbox.service import SandboxService, normalize_config
from rsi.sandbox.sshpool import (
    ExecResult,
    MockDeviceSession,
    SessionKey,
    SshPool,
)
from rsi.sandbox.vendor import VENDOR_MAP
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore

from tests.unit.test_batch_runner import (
    OUT,
    PROMPT,
    FakeJudge,
    _env,
    _task,
)


# ---------------------------------------------------------------- helpers


class SpyCarrier(HostService):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.created: list[dict] = []

    def create_instance(self, *a, **kw):
        self.created.append(dict(kw, _pos=a))
        return super().create_instance(*a, **kw)


class Rig:
    """Minimal offline BatchRunner rig (Fake backend + Fake carrier)."""

    def __init__(self, tmp_path: Path, instrumentor=None, session_factory=None,
                 carrier_cls=HostService) -> None:
        self.backend = FakeSandboxBackend()
        self.sandbox = SandboxService(
            backends={"fake": self.backend}, labs_dir=tmp_path,
            session_factory=session_factory,
        )
        self.carrier = carrier_cls(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
        )
        self.judge = FakeJudge()
        self.collector = TraceCollector(SeqAllocator(), None)
        self.trace_store = TraceStore(self.collector)
        self.runner = BatchRunner(
            sandbox=self.sandbox,
            carrier=self.carrier,
            collector=self.collector,
            trace_store=self.trace_store,
            judge=self.judge,
            ckpt=CheckpointStore(),
            budget=BudgetController(
                stop_file=tmp_path / "STOP",
                scope_limits={"b-1": 10**7, "b-2": 10**7},
                global_limit=10**9,
            ),
            event_bus=EventBus(),
            stats_agg=StatsAggregator(),
            instrumentor=instrumentor,
            session_factory=session_factory,
        )


def _sqlite_factory():
    from sqlmodel import SQLModel
    from rsi.db import create_engine, get_session

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine, lambda: get_session(engine)


# ---------------------------------------------------------------- B-1


class TestB1InstrumentorWiring:
    def test_stub_instrumentor_overlay_used_for_create_instance(self, tmp_path):
        overlay = tmp_path / "stub-overlay"
        overlay.mkdir()
        (overlay / "marker.txt").write_text("x", encoding="utf-8")
        report = CoverageReport(
            covered=[CoveredItem(aspect="tool", method="hook", files=["a.ts"])],
            not_instrumentable=[],
        )

        class StubInstrumentor:
            def __init__(self):
                self.calls: list[tuple[str, str]] = []

            def instrument(self, config_dir, carrier, smoke_fn):
                self.calls.append((str(config_dir), carrier))
                return overlay, report

        config_dir = tmp_path / "cv-1"
        config_dir.mkdir()
        stub = StubInstrumentor()
        rig = Rig(tmp_path, instrumentor=stub, carrier_cls=SpyCarrier)
        stats = asyncio.run(
            rig.runner.run_batch(
                "b-1", _env(), [_task()], config_version_id=str(config_dir),
                image_ref="sut:1",
            )
        )
        assert stats.n == 1
        # instrument called with the exported config + carrier type
        assert stub.calls == [(str(config_dir), "opencode")]
        # every create_instance (task run) received the instrumented overlay
        assert rig.carrier.created, "create_instance was never called"
        for call in rig.carrier.created:
            if call.get("run_id") == "instrument-smoke":
                # smoke runs are NOT instrumented and use the config dir as-is
                continue
            assert call["config_version_id"] == str(overlay)
            assert call["instrument"] is True
        _ = stats

    def test_none_instrumentor_keeps_legacy_behavior(self, tmp_path):
        config_dir = tmp_path / "cv-1"
        config_dir.mkdir()
        rig = Rig(tmp_path, instrumentor=None, carrier_cls=SpyCarrier)
        asyncio.run(
            rig.runner.run_batch(
                "b-1", _env(), [_task()], config_version_id=str(config_dir),
                image_ref="sut:1",
            )
        )
        assert rig.carrier.created
        for call in rig.carrier.created:
            assert call["config_version_id"] == str(config_dir)


# ---------------------------------------------------------------- B-2


class TestB2DbWrites:
    def test_build_and_destroy_write_labs_row(self, tmp_path):
        from rsi.db.tables import Lab
        from sqlmodel import Session, select

        engine, factory = _sqlite_factory()
        service = SandboxService(
            backends={"fake": FakeSandboxBackend()},
            labs_dir=tmp_path,
            session_factory=factory,
        )
        lab = asyncio.run(service.build_lab(_env()))
        with Session(engine) as s:
            row = s.exec(select(Lab).where(Lab.id == lab.lab_id)).one()
        assert row.status == "ready"
        assert row.backend == "fake"
        endpoints = json.loads(row.device_endpoints_json)
        assert endpoints["R1"]["ip"] == "10.0.0.1"
        assert endpoints["R1"]["port"] == 2001
        assert row.devices_online == "2/2"
        assert row.env_hash

        asyncio.run(service.destroy(lab))
        with Session(engine) as s:
            row = s.exec(select(Lab).where(Lab.id == lab.lab_id)).one()
        assert row.status == "destroyed"
        assert row.destroyed_at

    def test_instrumentation_report_row_after_batch(self, tmp_path):
        from rsi.db.tables import InstrumentationReport
        from sqlmodel import Session, select

        engine, factory = _sqlite_factory()
        overlay = tmp_path / "ov"
        overlay.mkdir()
        config_dir = tmp_path / "cv-9"
        config_dir.mkdir()
        report = CoverageReport(
            covered=[CoveredItem(aspect="tool", method="mcp_wrap", files=["mcp.json"])],
            not_instrumentable=[
                NotInstrumentable(aspect="llm", reason="I-8 excluded")
            ],
        )

        class StubInstrumentor:
            def instrument(self, config_dir, carrier, smoke_fn):
                return overlay, report

        rig = Rig(tmp_path, instrumentor=StubInstrumentor(), session_factory=factory)
        asyncio.run(
            rig.runner.run_batch(
                "b-1", _env(), [_task()], config_version_id=str(config_dir),
                image_ref="sut:1",
            )
        )
        with Session(engine) as s:
            rows = s.exec(
                select(InstrumentationReport).where(
                    InstrumentationReport.batch_id == "b-1"
                )
            ).all()
        assert len(rows) == 1  # once per (batch, config_version)
        cov = json.loads(rows[0].coverage_json)
        assert cov["covered"][0]["method"] == "mcp_wrap"
        plan = json.loads(rows[0].plan_json)
        assert plan["config_version_id"] == str(config_dir)


# ---------------------------------------------------------------- B-3


class _SlowRunner:
    """Duck-typed BatchRunner: sleeps then records concurrency."""

    def __init__(self, sleep_s: float, tracker: dict) -> None:
        self.sleep_s = sleep_s
        self.tracker = tracker

    async def run_batch(self, batch_id: str, **kw):
        self.tracker["active"] = self.tracker.get("active", 0) + 1
        self.tracker["peak"] = max(self.tracker.get("peak", 0), self.tracker["active"])
        await asyncio.sleep(self.sleep_s)
        self.tracker["active"] -= 1
        return SimpleNamespace(batch_id=batch_id)


class TestB3ParallelRunner:
    def test_two_batches_run_concurrently(self):
        tracker: dict = {}
        runner = ParallelBatchRunner(_SlowRunner(0.2, tracker), max_parallel=2)
        start = time.perf_counter()
        results = asyncio.run(
            runner.run_many([{"batch_id": "b-1"}, {"batch_id": "b-2"}])
        )
        elapsed = time.perf_counter() - start
        assert [r.batch_id for r in results] == ["b-1", "b-2"]
        assert tracker["peak"] == 2
        assert elapsed < 0.38, f"batches serialized: {elapsed:.2f}s"

    def test_max_parallel_one_serializes(self):
        tracker: dict = {}
        runner = ParallelBatchRunner(_SlowRunner(0.2, tracker), max_parallel=1)
        start = time.perf_counter()
        asyncio.run(runner.run_many([{"batch_id": "b-1"}, {"batch_id": "b-2"}]))
        elapsed = time.perf_counter() - start
        assert tracker["peak"] == 1
        assert elapsed >= 0.38

    def test_default_max_parallel_from_settings(self):
        from rsi.settings import Settings

        settings = Settings()
        runner = ParallelBatchRunner(
            SimpleNamespace(settings=settings, run_batch=None),
            settings=settings,
        )
        assert runner.max_parallel == settings.orchestrator.max_parallel_batches


# ---------------------------------------------------------------- B-4


class TestB4SshPoolAlive:
    def test_dead_session_is_rebuilt(self):
        made: list[MockDeviceSession] = []

        def factory(key, vendor):
            session = MockDeviceSession(key, vendor)
            made.append(session)
            return session

        # simpler: first session alive, then kill it
        pool = SshPool(session_factory=factory)
        key = SessionKey(ip="10.0.0.1", port=22, lab_id="L1")
        access = SshAccess(ip="10.0.0.1", username="a")
        s1 = asyncio.run(pool.device(key, access, "vrp"))
        assert made and s1 is made[0]
        s1.is_alive = lambda: False  # type: ignore[method-assign]
        s2 = asyncio.run(pool.device(key, access, "vrp"))
        assert s2 is not s1
        assert len(made) == 2  # rebuilt once

    def test_alive_session_is_reused(self):
        made: list[MockDeviceSession] = []

        def factory(key, vendor):
            s = MockDeviceSession(key, vendor)
            made.append(s)
            return s

        pool = SshPool(session_factory=factory)
        key = SessionKey(ip="10.0.0.2", port=22, lab_id="L1")
        access = SshAccess(ip="10.0.0.2", username="a")
        s1 = asyncio.run(pool.device(key, access, "vrp"))
        s2 = asyncio.run(pool.device(key, access, "vrp"))
        assert s1 is s2 and len(made) == 1


# ---------------------------------------------------------------- B-5


class TestB5VerifyImage:
    def test_offline_returns_unknown(self):
        svc = HostService(
            host_pool=HostPool(),  # offline stub -> client() is None
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier()},
        )
        check = asyncio.run(svc.verify_image("sut:v1"))
        assert check.ok is None
        assert check.image_ref == "sut:v1"
        assert "offline" in check.detail

    def test_missing_image_ok_false(self):
        class FakeImages:
            def get(self, ref):
                raise KeyError(ref)

        class FakeClient:
            images = FakeImages()

        class Pool(HostPool):
            def client(self, host):
                return FakeClient()

        svc = HostService(
            host_pool=Pool(), net_policy=NetPolicy(), adapters={"fake": FakeCarrier()}
        )
        check = asyncio.run(svc.verify_image("sut:missing"))
        assert check.ok is False
        assert "docker inspect" in check.checks

    def test_present_image_ok_true(self):
        class FakeClient:
            class images:
                @staticmethod
                def get(ref):
                    return SimpleNamespace(id=ref)

        class Pool(HostPool):
            def client(self, host):
                return FakeClient()

        svc = HostService(
            host_pool=Pool(), net_policy=NetPolicy(), adapters={"fake": FakeCarrier()}
        )
        check = asyncio.run(svc.verify_image("sut:ok"))
        assert check.ok is True


# ---------------------------------------------------------------- B-6


class _RecordingSession(MockDeviceSession):
    def __init__(self, key, vendor, responses=None, config_text=""):
        super().__init__(key, vendor, responses or {})
        self.config_calls: list[list[str]] = []
        self._config_text = config_text

    async def exec_config(self, lines):
        self.config_calls.append(list(lines))
        return ExecResult(ok=True)

    async def capture_config(self):
        return self._config_text


class _SingleSessionPool(SshPool):
    def __init__(self, session) -> None:
        super().__init__()
        self._session = session

    async def device(self, key, access, vendor):
        return self._session


def _env_one() -> EnvSpec:
    return EnvSpec(
        name="lab-b6",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R1:GE0/0/2"))]),
        ssh_access={"R1": SshAccess(ip="10.0.0.9", port=2022, username="admin")},
    )


class TestB6VendorReset:
    def _applied(self, backup_path: Path):
        from datetime import UTC, datetime

        from rsi.core.models import AppliedAction
        return [
            AppliedAction(
                action="exec_cli",
                entries=[CliEntry(device="R1", cli_lines=["sysname X"])],
                backup_files={"R1": str(backup_path)},
                applied_at=datetime.now(UTC).isoformat(),
            )
        ]

    def test_pool_path_uses_vendor_reset_cmd_not_lab_reset(self, tmp_path):
        backup = tmp_path / "R1.cfg"
        backup.write_text("sysname R1\ninterface GE0/0/1\n", encoding="utf-8")
        session = _RecordingSession(
            SessionKey("10.0.0.9", 2022, "L1"),
            VENDOR_MAP["vrp"],
            config_text=backup.read_text(encoding="utf-8"),
        )
        backend = FakeSandboxBackend()
        resets: list[str] = []
        orig_reset = backend.reset

        async def spy_reset(lab):
            resets.append(lab.lab_id)
            return await orig_reset(lab)

        backend.reset = spy_reset  # type: ignore[method-assign]
        service = SandboxService(
            backends={"fake": backend},
            labs_dir=tmp_path,
            ssh_pool=_SingleSessionPool(session),
        )
        lab = asyncio.run(service.build_lab(_env_one()))
        asyncio.run(service.restore_task(lab, self._applied(backup)))
        # vendor reset_cmd went through the pooled session...
        assert session.config_calls
        reset_lines = [c for calls in session.config_calls for c in calls]
        assert "reset saved-configuration" in reset_lines
        # ...and the whole-lab backend reset was never invoked
        assert resets == []

    def test_equivalence_mismatch_raises_and_retries(self, tmp_path):
        backup = tmp_path / "R1.cfg"
        backup.write_text("sysname R1\ninterface GE0/0/1\n", encoding="utf-8")
        session = _RecordingSession(
            SessionKey("10.0.0.9", 2022, "L1"),
            VENDOR_MAP["vrp"],
            config_text="totally different config\n",
        )
        service = SandboxService(
            backends={"fake": FakeSandboxBackend()},
            labs_dir=tmp_path,
            ssh_pool=_SingleSessionPool(session),
        )
        lab = asyncio.run(service.build_lab(_env_one()))
        from rsi.core.errors import EnvError

        with pytest.raises(EnvError):
            asyncio.run(service.restore_task(lab, self._applied(backup)))
        # 3 attempts (1 + 2 retries), each ending in the mismatch
        assert len(session.config_calls) >= 3

    def test_normalize_config_drops_volatile_lines(self):
        text = "# comment\n!\n<R1>display clock\n2026-01-01 00:00:00 UTC\nsysname R1\n\n"
        assert normalize_config(text) == ["sysname R1"]


# ---------------------------------------------------------------- B-7


class TestB7HealthTimestamp:
    @pytest.mark.asyncio
    async def test_pool_output_without_timestamp_unhealthy(self, tmp_path):
        class R:
            ok = True
            output = "device up, no clock\n"
            matched_errors = []

        class _S:
            async def exec_cli(self, cli):
                return R()

        class _P(SshPool):
            async def device(self, key, access, vendor):
                return _S()

        service = SandboxService(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path, ssh_pool=_P()
        )
        lab = await service.build_lab(_env())
        assert await service.health_check(lab) is False

    @pytest.mark.asyncio
    async def test_pool_output_with_timestamp_healthy(self, tmp_path):
        class R:
            ok = True
            output = "2026-11-05 10:00:00\nUTC\n"
            matched_errors = []

        class _S:
            async def exec_cli(self, cli):
                return R()

        class _P(SshPool):
            async def device(self, key, access, vendor):
                return _S()

        service = SandboxService(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path, ssh_pool=_P()
        )
        lab = await service.build_lab(_env())
        assert await service.health_check(lab) is True

    @pytest.mark.asyncio
    async def test_backend_output_without_timestamp_unhealthy(self, tmp_path):
        class NoClockBackend(FakeSandboxBackend):
            def exec(self, lab, device, cli):
                if "clock" in cli:
                    return "still booting\n"
                return super().exec(lab, device, cli)

        service = SandboxService(
            backends={"fake": NoClockBackend()}, labs_dir=tmp_path
        )
        lab = await service.build_lab(_env())
        assert await service.health_check(lab) is False


# ---------------------------------------------------------------- B-9


class TestB9CreateInstanceCleanup:
    def test_attach_failure_cleans_overlay_and_count(self, tmp_path):
        class BoomPolicy(NetPolicy):
            def attach(self, host, container_id, nets):
                raise RuntimeError("net boom")

        config_dir = tmp_path / "sut-cfg"
        config_dir.mkdir()
        (config_dir / "a.txt").write_text("a", encoding="utf-8")
        pool = HostPool()
        svc = HostService(
            host_pool=pool,
            net_policy=BoomPolicy(pool),
            adapters={"fake": FakeCarrier()},
        )
        from rsi.core.models import LabHandle

        lab = LabHandle(
            lab_id="L9", host="main", backend="fake",
            endpoints={"R1": SshAccess(ip="10.0.0.1", username="a")},
        )
        with pytest.raises(RuntimeError):
            svc.create_instance(
                image_ref="sut", config_version_id=str(config_dir), lab=lab,
                permission="read-only", instrument=True, host="main",
            )
        # overlay directory cleaned up
        assert list(tmp_path.glob("overlay-*")) == []
        # host count not incremented
        assert pool._instance_counts["main"] == 0
        assert svc._instances == {}


# ---------------------------------------------------------------- B-10


class TestB10ExitCode:
    def _svc(self, tmp_path):
        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses={PROMPT: OUT})},
        )
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(exist_ok=True)
        from rsi.core.models import LabHandle

        lab = LabHandle(
            lab_id="L10", host="main", backend="fake",
            endpoints={"R1": SshAccess(ip="10.0.0.1", username="a")},
        )
        inst = svc.create_instance(
            image_ref="sut", config_version_id=str(config_dir), lab=lab,
            permission="read-only", instrument=False, host="main", run_id="r10",
        )
        return svc, inst

    def test_exit_137_marks_crashed_no_extraction(self, tmp_path):
        svc, inst = self._svc(tmp_path)
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        handle.exit_code = 137
        result = svc.collect(handle)
        assert result.instance_crashed is True
        assert result.task_complete is False
        assert result.root_cause is None  # FINAL_JSON not extracted
        svc.destroy(inst)

    def test_exit_139_marks_crashed(self, tmp_path):
        svc, inst = self._svc(tmp_path)
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        handle.exit_code = 139
        assert svc.collect(handle).instance_crashed is True
        svc.destroy(inst)

    def test_exit_zero_with_stdout_still_extracts(self, tmp_path):
        svc, inst = self._svc(tmp_path)
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        handle.exit_code = 0
        result = svc.collect(handle)
        assert result.task_complete is True
        assert result.root_cause == "bfd timer mismatch"
        svc.destroy(inst)

    def test_exit_zero_empty_stdout_parse_error(self, tmp_path):
        svc = HostService(
            host_pool=HostPool(), net_policy=NetPolicy(),
            adapters={"fake": FakeCarrier(responses={})},
        )
        from rsi.core.models import LabHandle

        lab = LabHandle(
            lab_id="L10b", host="main", backend="fake",
            endpoints={"R1": SshAccess(ip="10.0.0.1", username="a")},
        )
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        inst = svc.create_instance(
            image_ref="sut", config_version_id=str(config_dir), lab=lab,
            permission="read-only", instrument=False, host="main", run_id="r10b",
        )
        handle = svc.submit_task(inst, TaskSpec(prompt="no canned response"))
        handle.exit_code = 0
        result = svc.collect(handle)
        assert result.parse_error is True
        assert result.instance_crashed is False
        svc.destroy(inst)

    def test_runhandle_has_exit_code_field(self):
        h = RunHandle(run_id="r", instance_id="i", cmd=["c"])
        assert h.exit_code is None


# ---------------------------------------------------------------- B-12


class TestB12BootstrapEvent:
    def test_bootstrap_event_is_not_tool_traffic(self, tmp_path):
        from rsi.instrumentor.injector import Injector
        from rsi.instrumentor.verifier import Verifier
        from rsi.core.models import TaskResult

        sink = tmp_path / "trace" / "events.ndjson"
        sink.parent.mkdir(parents=True)
        Injector._init_sink(tmp_path)
        line = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
        assert line["event_type"] == "instrumentation_applied"
        # verifier no longer credits the bootstrap marker
        ok = TaskResult(task_complete=True, root_cause="x")
        r = Verifier.verify(ok, ok, [line])
        assert r.events_produced is False and not r.passed
        # ...but real tool events pass
        r2 = Verifier.verify(
            ok, ok, [line, {"event_type": "tool_call", "data": {}}]
        )
        assert r2.events_produced is True and r2.passed


# ---------------------------------------------------------------- B-15


class TestB15ReadinessFallback:
    @pytest.mark.asyncio
    async def test_backend_timeout_checker_fallback_passes(self, tmp_path):
        class NeverReadyBackend(FakeSandboxBackend):
            async def wait_ready(self, lab, timeout_s=600):
                return ReadyReport(passed=False, elapsed_s=float(timeout_s or 0))

        class StubChecker:
            async def check(self, env, endpoints, timeout_s=600):
                return ReadyReport(passed=True)

        service = SandboxService(
            backends={"fake": FakeSandboxBackend()},
            labs_dir=tmp_path,
            readiness_checker=StubChecker(),
        )
        lab = await service.build_lab(_env())
        # swap in a backend whose wait_ready always times out
        service._backends["fake"] = NeverReadyBackend()
        report = await service.wait_ready(lab, timeout_s=1)
        assert report.passed is True

    @pytest.mark.asyncio
    async def test_no_checker_returns_backend_report(self, tmp_path):
        class NeverReadyBackend(FakeSandboxBackend):
            async def wait_ready(self, lab, timeout_s=600):
                return ReadyReport(passed=False)

        service = SandboxService(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path
        )
        lab = await service.build_lab(_env())
        service._backends["fake"] = NeverReadyBackend()
        report = await service.wait_ready(lab, timeout_s=1)
        assert report.passed is False


# ---------------------------------------------------------------- B-18


class TestB18CarrierTypeDispatch:
    def test_opencode_type_gets_opencode_adapter(self, tmp_path):
        from rsi.carrier.adapters.opencode import OpenCodeAdapter

        opencode = OpenCodeAdapter()
        fake = FakeCarrier(responses={PROMPT: OUT})
        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"opencode": opencode, "fake": fake},
        )
        from rsi.core.models import LabHandle

        lab = LabHandle(
            lab_id="L18", host="main", backend="fake",
            endpoints={"R1": SshAccess(ip="10.0.0.1", username="a")},
        )
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        inst = svc.create_instance(
            image_ref="sut", config_version_id=str(config_dir), lab=lab,
            permission="read-only", instrument=False, host="main",
            carrier_type="opencode",
        )
        assert inst.carrier == "opencode"
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        assert handle is not None
        assert type(handle) is RunHandle  # both produce RunHandle; check cmd
        svc.destroy(inst)

    def test_fake_type_gets_fake_adapter(self, tmp_path):
        from rsi.carrier.adapters.opencode import OpenCodeAdapter

        opencode = OpenCodeAdapter()
        fake = FakeCarrier(responses={PROMPT: OUT})
        seen: dict[str, str] = {}

        class SpyFake(FakeCarrier):
            # note: carries the canned responses itself
            def submit_task(self, instance, task):
                seen["fake"] = instance.carrier
                return super().submit_task(instance, task)

        class SpyOpenCode(OpenCodeAdapter):
            def submit_task(self, instance, task):
                seen["opencode"] = instance.carrier
                return super().submit_task(instance, task)

        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"opencode": SpyOpenCode(), "fake": SpyFake({PROMPT: OUT})},
        )
        from rsi.core.models import LabHandle

        lab = LabHandle(
            lab_id="L18b", host="main", backend="fake",
            endpoints={"R1": SshAccess(ip="10.0.0.1", username="a")},
        )
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        inst = svc.create_instance(
            image_ref="sut", config_version_id=str(config_dir), lab=lab,
            permission="read-only", instrument=False, host="main",
            carrier_type="fake",
        )
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        assert seen == {"fake": "fake"}
        assert svc.collect(handle).root_cause == "bfd timer mismatch"
        svc.destroy(inst)
