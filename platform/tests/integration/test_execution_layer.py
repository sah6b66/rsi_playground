"""Execution-layer integration: Sandbox + Carrier + Tracing offline chain.

Real components only: FakeSandboxBackend (in-memory devices), FakeCarrier
(scripted SUT stdout), real TraceCollector/TraceStore/SeqAllocator and real
HostService/SandboxService wiring. No mocks beyond the offline Fakes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.enums import EventType
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    PrepareAction,
    SshAccess,
    StateAssert,
    TaskRecord,
    TaskSpec,
    Topology,
    TraceEvent,
)
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceFilter, TraceStore

RUN_ID = "run-001"
TASK_PROMPT = "Diagnose the outage and output FINAL_JSON."
SUT_STDOUT = (
    "diagnosing...\n"
    "<FINAL_JSON>"
    '{"task_complete": true, "root_cause": "GE0/0/1 shutdown", '
    '"actions": [{"action": "display current-configuration"}], "confidence": 0.9}'
    "</FINAL_JSON>"
)


def _build_env() -> EnvSpec:
    return EnvSpec(
        name="test",
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", username="a", password="p"),
            "R2": SshAccess(ip="10.0.0.2", username="a", password="p"),
        },
    )


def _build_task() -> TaskRecord:
    return TaskRecord(
        task_id="fault/test-01",
        title="Test",
        prepare=[
            PrepareAction(
                action="exec_cli",
                entries=[
                    {
                        "device": "R1",
                        "cli_lines": ["interface GE0/0/1", "shutdown"],
                    }
                ],
            )
        ],
        task=TaskSpec(prompt=TASK_PROMPT),
        expected=ExpectedAnswer(
            final_state=[
                StateAssert(
                    device="R1",
                    cli="display current-configuration",
                    expect_contains=["shutdown"],
                )
            ]
        ),
        metrics=[],
    )


def test_offline_execution_chain(tmp_path: Path) -> None:
    # ── 1. Setup: Sandbox + Carrier + Tracing, all real/offline ──────
    sandbox = SandboxService(
        backends={"fake": FakeSandboxBackend()},
        labs_dir=tmp_path / "labs",
    )
    carrier = HostService(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"opencode": FakeCarrier(responses={TASK_PROMPT: SUT_STDOUT})},
    )
    seq_alloc = SeqAllocator()
    collector = TraceCollector(seq_alloc=seq_alloc, store=None)
    store = TraceStore(collector=collector)
    collector._store = store  # wire back-reference (circular by design)

    env = _build_env()
    task = _build_task()

    # ── 2-3. Build lab (fake backend, snap0 taken by the service) ────
    lab = asyncio.run(sandbox.build_lab(env))
    assert set(lab.endpoints) == {"R1", "R2"}

    # ── 4-5. Prepare: inject the fault via exec_cli ──────────────────
    applied = asyncio.run(sandbox.prepare(lab, task.task_id, task.prepare))
    assert len(applied) == 1
    assert applied[0].action == "exec_cli"
    assert "R1" in applied[0].backup_files
    backup_path = Path(applied[0].backup_files["R1"])
    assert backup_path.is_file()

    # ── 6. Read-only exec shows the config change ────────────────────
    output = sandbox.exec_readonly(lab, "R1", "display current-configuration")
    assert "shutdown" in output
    assert "interface GE0/0/1" in output

    # ── 7. Carrier instance lifecycle ────────────────────────────────
    instance = carrier.create_instance(
        image_ref="test:latest",
        config_version_id=str(tmp_path / "cfg"),
        lab=lab,
        permission="read-only",
        instrument=False,
        host="main",
        cred=None,
        run_id=RUN_ID,
    )
    assert instance.env["RSI_LAB_ID"] == lab.lab_id

    # ── 8. Submit + collect: FINAL_JSON extracted into TaskResult ────
    handle = carrier.submit_task(instance, task.task)
    result = carrier.collect(handle, timeout_s=5)
    assert result.task_complete is True
    assert result.parse_error is False
    assert result.root_cause == "GE0/0/1 shutdown"

    # ── 9-10. Trace chain: ingest -> finalize -> query ───────────────
    event = TraceEvent(
        run_id=RUN_ID,
        task_id=task.task_id,
        lab_id=lab.lab_id,
        sut_id="test-agent",
        seq=0,
        ts="2026-11-04T10:00:00Z",
        event_type=EventType.TOOL_CALL,
        data={"tool": "ssh_cli", "args": {"command": "display clock"}},
    )
    collector.ingest([event])
    collector.finalize(RUN_ID)
    events, _cursor = store.query(TraceFilter(run_ids=[RUN_ID]), cursor=None, limit=50)
    assert len(events) == 1
    assert events[0].event_type == EventType.TOOL_CALL
    assert events[0].seq == 0
    assert store.get_meta(RUN_ID) == {
        "run_id": RUN_ID,
        "event_count": 1,
        "first_ts": "2026-11-04T10:00:00Z",
        "last_ts": "2026-11-04T10:00:00Z",
        "finalized": True,
    }

    # ── 11. Restore: replay pre-task backup, fault is gone ───────────
    asyncio.run(sandbox.restore_task(lab, applied))
    output_after = sandbox.exec_readonly(lab, "R1", "display current-configuration")
    assert "shutdown" not in output_after
    assert "interface GE0/0/1" not in output_after

    # ── 12. Tear down lab + instance ─────────────────────────────────
    asyncio.run(sandbox.destroy(lab))
    carrier.destroy(instance)
