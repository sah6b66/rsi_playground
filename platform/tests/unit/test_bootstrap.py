"""TU for the production composition root (audit T2-1/T2-2/T2-4/B-16)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import SQLModel, select

from rsi.bootstrap import build_services
from rsi.settings import DatabaseSettings, Settings

EXPECTED_KEYS = {
    "settings",
    "engine",
    "session_factory",
    "auth_service",
    "audit_log",
    "credential_store",
    "credential_resolver",
    "git_store",
    "tracing_collector",
    "trace_store",
    "prompt_library",
    "accountant",
    "llm_client",
    "llm_judge",
    "judge_service",
    "host_service",
    "host_pool",
    "net_policy",
    "sandbox_service",
    "ssh_pool",
    "stats_agg",
    "selector",
    "noise_band",
    "decision_recorder",
    "target_registry",
    "attribution_analyzer",
    "deterministic_critic",
    "change_applier",
    "sft_assessor",
    "event_bus",
    "checkpoint_store",
    "budget_controller",
    "batch_runner",
    "iteration_engine",
    "users",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path,
        database=DatabaseSettings(url=f"sqlite:///{tmp_path/'test.db'}"),
    )


def test_build_services_offline_safe() -> None:
    """Default Settings assembly: no docker/LLM/key/network required."""
    services = build_services()
    assert EXPECTED_KEYS <= set(services.keys())
    # cred chain wired (T2-1)
    assert services["batch_runner"].credential_resolver is services[
        "credential_resolver"
    ]
    # judgments persistence wired (T2-2)
    assert services["judge_service"]._session_factory is services["session_factory"]
    # collector <-> store circular dep established via public API
    assert services["tracing_collector"]._store is services["trace_store"]
    # API DI keys present (T2-4)
    for key in ("auth_service", "audit_log", "batch_runner", "users"):
        assert key in services


@pytest.mark.asyncio
async def test_judge_service_wired_with_session_factory(tmp_path: Path) -> None:
    """judge_run through the composed JudgeService persists a Judgment row."""
    from rsi.core.enums import EventType
    from rsi.core.models import (
        DeviceSpec,
        EnvSpec,
        EvalTask,
        ExpectedAnswer,
        RootCauseExpectation,
        SshAccess,
        TaskRecord,
        TaskSpec,
        TaskResult,
        ExpectedAnswer,
        RootCauseExpectation,
        Topology,
        TraceEvent,
    )
    from rsi.db.tables import Judgment

    services = build_services(_settings(tmp_path))
    SQLModel.metadata.create_all(services["engine"])

    env = EnvSpec(
        name="env-j",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(),
        ssh_access={"R1": SshAccess(ip="127.0.0.1", username="u", password="pw")},
    )
    task = EvalTask(
        env=env,
        task=TaskRecord(
            task_id="fault/boot-01",
            title="bootstrap judge",
            task=TaskSpec(prompt="p"),
            expected=ExpectedAnswer(
                root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
            ),
        ),
    )
    events = [
        TraceEvent(
            run_id="run-boot-1",
            task_id="fault/boot-01",
            lab_id="lab-1",
            sut_id="sut",
            seq=0,
            ts="2026-01-01T00:00:00.000+00:00",
            event_type=EventType.TOOL_CALL,
            data={"args": {"command": "display current-configuration"}},
        )
    ]
    verdict = await services["judge_service"].judge_run(
        task, TaskResult(task_complete=True), events, None, None, "read-only"
    )
    assert verdict is not None
    with services["session_factory"]() as session:
        rows = session.exec(select(Judgment)).all()
    assert len(rows) == 1
    assert rows[0].run_id == "run-boot-1"


@pytest.mark.asyncio
async def test_batch_runner_has_cred_resolver(tmp_path: Path) -> None:
    """Resolved creds flow into InstanceHandle.env as DEV_* vars (T2-1)."""
    from rsi.carrier.adapters.fake import FakeCarrier
    from rsi.carrier.hosts import HostPool
    from rsi.carrier.network import NetPolicy
    from rsi.carrier.service import HostService
    from rsi.core.models import (
        DeviceSpec,
        Efficiency,
        EnvSpec,
        ExpectedAnswer,
        PathQuality,
        SafetyVerdict,
        Scores,
        Verdict,
        LinkSpec,
        RootCauseExpectation,
        SshAccess,
        TaskRecord,
        TaskSpec,
        Topology,
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

    def _verdict() -> Verdict:
        return Verdict(
            scores=Scores(
                root_cause_accuracy=0.5,
                task_complete=True,
                diagnostic_steps=1,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
            efficiency=Efficiency(),
            safety=SafetyVerdict(),
            missing=False,
        )

    class _FakeJudge:
        async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
            return _verdict()

    class StubResolver:
        def resolve_env(self, env, permission):
            return {
                "devices": {
                    "R1": {
                        "user": "admin",
                        "pass": "s3cret",
                        "host": "10.0.0.1",
                        "port": "2001",
                    }
                }
            }

    sandbox = SandboxService(backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path)
    carrier = HostService(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"fake": FakeCarrier()},
    )
    collector = TraceCollector(SeqAllocator(), None)
    created: list = []
    original_create = carrier.create_instance

    def recording_create(**kwargs):
        handle = original_create(**kwargs)
        created.append(handle)
        return handle

    carrier.create_instance = recording_create  # type: ignore[assignment]
    runner = BatchRunner(
        sandbox=sandbox,
        carrier=carrier,
        collector=collector,
        trace_store=TraceStore(collector),
        judge=_FakeJudge(),
        ckpt=CheckpointStore(),
        budget=BudgetController(tmp_path / "STOP", {}, 10**9),
        event_bus=EventBus(),
        stats_agg=StatsAggregator(),
        credential_resolver=StubResolver(),
    )
    assert runner.credential_resolver is not None

    env = EnvSpec(
        name="lab-cred",
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
        ],
        topology=Topology(links=[]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
        },
    )
    tasks = [
        TaskRecord(
            task_id="fault/cred-01",
            title="t",
            task=TaskSpec(prompt="p"),
            expected=ExpectedAnswer(
                root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
            ),
        )
    ]
    await runner.run_batch("batch-cred", env, tasks, "cfg-v1", "sut-img", "read-only")
    assert created, "create_instance was never called"
    env_vars = created[0].env
    assert env_vars["DEV_R1_PASS"] == "s3cret"
    assert env_vars["DEV_R1_USER"] == "admin"
    assert env_vars["DEV_R1_HOST"] == "10.0.0.1"
    assert env_vars["DEV_R1_PORT"] == "2001"
