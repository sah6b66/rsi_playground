"""IN-023 (ADR-27 D7): dual-mode end-to-end integration.

API mode: stub EvalsetFetcher -> api_task_to_task_record -> run_batch
(prep_mode="api") with a stub NetworkPrepClient — full chain asserted
(prepare x2 / restore x2 / zero sandbox build_lab / cred=None into the
carrier / judgments persisted / batch finalized).

CLI mode regression on the same rig: prep_mode="cli" keeps the lab
pipeline (build_lab >= 1) and never touches the netprep client.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, select

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.models import ApiEvalsetTask, NetworkEnvRef
from rsi.db.tables import Judgment
from rsi.decision.aggregator import StatsAggregator
from rsi.orchestrator.batch import BatchRunner, api_task_to_task_record
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.checkpoint import CheckpointStore
from rsi.orchestrator.event_bus import EventBus
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.netprep import PrepResult, RestoreResult
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceStore
from tests.unit.test_batch_runner import OUT, PROMPT, FakeJudge, _env, _task

pytestmark = pytest.mark.asyncio


class StubNetprep:
    """NetworkPrepClient stub recording prepare/restore calls (D4 contract)."""

    def __init__(self) -> None:
        self.prepare_calls: list[tuple[str, dict, str]] = []
        self.restore_calls: list[tuple[str, str, dict]] = []

    async def prepare(self, env_name: str, params: dict, session_id: str = "") -> PrepResult:
        self.prepare_calls.append((env_name, params, session_id))
        return PrepResult(env_id=f"env-{len(self.prepare_calls)}")

    async def restore(self, env_name: str, env_id: str, params: dict) -> RestoreResult:
        self.restore_calls.append((env_name, env_id, params))
        return RestoreResult()

    async def aclose(self) -> None:
        pass


class StubEvalsetFetcher:
    """EvalsetFetcher stub returning 2 ApiEvalsetTasks (distinct env refs)."""

    async def fetch(self, name: str) -> list[ApiEvalsetTask]:
        from rsi.core.models import ExpectedAnswer, RootCauseExpectation, TaskSpec

        return [
            ApiEvalsetTask(
                task_id="fault/api-01",
                title="api task 1",
                network_env=NetworkEnvRef(
                    name="lab-bgp-3node", params={"topology": "triangle"}
                ),
                task=TaskSpec(prompt=PROMPT),
                expected=ExpectedAnswer(
                    root_cause=RootCauseExpectation(expected="bfd timer mismatch")
                ),
            ),
            ApiEvalsetTask(
                task_id="fault/api-02",
                title="api task 2",
                network_env=NetworkEnvRef(
                    name="lab-ospf-4node", params={"topology": "ring", "fault": "cost"}
                ),
                task=TaskSpec(prompt=PROMPT),
                expected=ExpectedAnswer(
                    root_cause=RootCauseExpectation(expected="bfd timer mismatch")
                ),
            ),
        ]

    async def aclose(self) -> None:
        pass


class CredRecordingHostService(HostService):
    """HostService recording the cred payload each create_instance got."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.create_creds: list[object] = []

    def create_instance(self, *args, **kw):
        self.create_creds.append(kw.get("cred", "<missing>"))
        return super().create_instance(*args, **kw)


class CountingSandbox:
    """Delegating SandboxService wrapper counting the lab pipeline calls."""

    def __init__(self, inner: SandboxService) -> None:
        self._inner = inner
        self.build_lab_calls = 0
        self.prepare_calls = 0
        self.restore_task_calls = 0
        self.destroy_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def build_lab(self, env):
        self.build_lab_calls += 1
        return await self._inner.build_lab(env)

    async def prepare(self, lab, task_id, actions):
        self.prepare_calls += 1
        return await self._inner.prepare(lab, task_id, actions)

    async def restore_task(self, lab, applied):
        self.restore_task_calls += 1
        return await self._inner.restore_task(lab, applied)

    async def destroy(self, lab):
        self.destroy_calls += 1
        return await self._inner.destroy(lab)


class PersistingJudge(FakeJudge):
    """FakeJudge that also writes a Judgment row per call (落库断言)."""

    def __init__(self, session_factory, run_id_of) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._run_id_of = run_id_of

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        verdict = await super().judge_run(task, result, events, lab, sandbox, permission, **kw)
        import uuid

        task_id = task.task.task_id if task is not None else None
        with self._session_factory() as session:
            session.add(
                Judgment(
                    id=str(uuid.uuid4()),
                    run_id=self._run_id_of(task_id),
                    det_json="{}",
                    verdict_json="{}",
                    created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                )
            )
            session.commit()
        return verdict


class DualModeRig:
    def __init__(self, tmp_path: Path) -> None:
        from rsi.settings import DatabaseSettings, Settings

        os.environ["RSI_HOME"] = str(tmp_path)
        db_url = f"sqlite:///{tmp_path / 'dualmode.db'}"
        # construct Settings against the temp db (validates the URL shape)
        Settings(database=DatabaseSettings(url=db_url))
        from sqlmodel import Session

        from rsi.db import create_engine

        engine = create_engine(db_url)
        SQLModel.metadata.create_all(engine)
        self.session_factory = lambda: Session(engine)

        self.backend = FakeSandboxBackend()
        self.sandbox = CountingSandbox(
            SandboxService(backends={"fake": self.backend}, labs_dir=tmp_path)
        )
        self.carrier = CredRecordingHostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"opencode": FakeCarrier({PROMPT: OUT})},
        )
        self.netprep = StubNetprep()
        self.fetcher = StubEvalsetFetcher()
        self.runner_placeholder = None  # judge needs runner.last_run_ids below
        self.judge = PersistingJudge(
            self.session_factory, lambda task_id: self.runner.last_run_ids.get(task_id, "")
        )
        self.collector = TraceCollector(SeqAllocator(), None)
        self.trace_store = TraceStore(self.collector)
        self.ckpt = CheckpointStore()
        self.budget = BudgetController(
            stop_file=tmp_path / "STOP",
            scope_limits={"batch-api": 10_000_000, "batch-cli": 10_000_000},
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
            session_factory=self.session_factory,
            carrier_type="opencode",
            netprep=self.netprep,
        )


@pytest.fixture
async def rig(tmp_path: Path) -> DualModeRig:
    return DualModeRig(tmp_path)


async def test_api_mode_end_to_end(tmp_path: Path) -> None:
    """IN-023: prep_mode='api' routes env lifecycle through netprep only."""
    rig = DualModeRig(tmp_path)
    api_tasks = await rig.fetcher.fetch("evalset-bgp")
    assert len(api_tasks) == 2
    tasks = [api_task_to_task_record(at) for at in api_tasks]
    assert all(t.prepare == [] for t in tasks)  # D4: no CLI prepare actions

    # scrub any DEV_* leakage vector before the run
    dev_vars = {k: v for k, v in os.environ.items() if k.startswith("DEV_")}
    for k in dev_vars:
        del os.environ[k]

    stats = await rig.runner.run_batch(
        "batch-api", None, tasks, "cfg-v1", "sut-img", "read-only",
        prep_mode="api",
    )

    # f. batch completed both tasks
    assert stats.n == 2
    assert rig.runner.last_status == "finalized"

    # a. netprep.prepare called twice, distinct env names, session_id=batch
    assert len(rig.netprep.prepare_calls) == 2
    assert [c[0] for c in rig.netprep.prepare_calls] == [
        "lab-bgp-3node", "lab-ospf-4node",
    ]
    assert all(c[2] == "batch-api" for c in rig.netprep.prepare_calls)
    assert rig.netprep.prepare_calls[0][1] == {"topology": "triangle"}
    assert rig.netprep.prepare_calls[1][1] == {"topology": "ring", "fault": "cost"}

    # b. netprep.restore called twice, env_ids match the prepare results
    assert len(rig.netprep.restore_calls) == 2
    assert [c[1] for c in rig.netprep.restore_calls] == ["env-1", "env-2"]
    assert [c[0] for c in rig.netprep.restore_calls] == [
        "lab-bgp-3node", "lab-ospf-4node",
    ]

    # c. sandbox lab pipeline entirely skipped (no lab in API mode)
    assert rig.sandbox.build_lab_calls == 0
    assert rig.sandbox.prepare_calls == 0
    assert rig.sandbox.restore_task_calls == 0
    assert rig.sandbox.destroy_calls == 0

    # d. carrier.create_instance got cred=None — no DEV_* injection (D4/ADR-27)
    assert len(rig.carrier.create_creds) == 2
    assert rig.carrier.create_creds == [None, None]
    # and no DEV_* leaked into the process environment either
    assert not {k for k in os.environ if k.startswith("DEV_")}

    # e. judgments persisted (stub judge writes a row per call)
    with rig.session_factory() as session:
        rows = session.exec(select(Judgment)).all()
    assert len(rows) == 2


async def test_api_mode_without_netprep_fails_loudly(tmp_path: Path) -> None:
    """prep_mode='api' with netprep=None raises EnvError (D4 guard)."""
    from rsi.core.errors import EnvError

    rig = DualModeRig(tmp_path)
    rig.runner.netprep = None
    api_tasks = await rig.fetcher.fetch("evalset-bgp")
    tasks = [api_task_to_task_record(at) for at in api_tasks]
    with pytest.raises(EnvError):
        await rig.runner.run_batch(
            "batch-api2", None, tasks, "cfg-v1", "sut-img", "read-only",
            prep_mode="api",
        )
    assert rig.netprep.prepare_calls == []


async def test_cli_mode_regression_lab_pipeline(tmp_path: Path) -> None:
    """CLI mode on the same rig keeps the sandbox lab pipeline, no netprep."""
    rig = DualModeRig(tmp_path)
    stats = await rig.runner.run_batch(
        "batch-cli", _env(), [_task(1), _task(2)], "cfg-v1", "sut-img", "read-only",
        prep_mode="cli",
    )
    assert stats.n == 2
    assert rig.runner.last_status == "finalized"

    # lab built once per batch, CLI prepare/restore ran per task
    assert rig.sandbox.build_lab_calls == 1
    assert rig.sandbox.prepare_calls == 2
    assert rig.netprep.prepare_calls == []
    assert rig.netprep.restore_calls == []
    assert len(rig.judge.calls) == 2
