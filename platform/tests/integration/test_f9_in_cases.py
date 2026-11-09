"""F9 integration coverage: IN-012 (env reuse + task restore) and
IN-014 (OpenCode wiretap -> collector -> store chain).

IN-012: 3-task batch asserts the full combination — build_lab called exactly
once, restore runs between tasks (before the next prepare), and a restore
failure degrades that task to env_failed while later tasks still execute.

IN-014: wire-level capture completeness — fixture NDJSON frames (assembled +
http) parsed by OpenCodeWiretapParser, ingested into a real TraceCollector /
TraceStore; llm_request full text lands in the store un-truncated.

Offline only: FakeSandboxBackend + FakeCarrier + mock judge. No LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService
from rsi.core.enums import EventType
from rsi.core.errors import EnvError
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    PrepareAction,
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
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.service import SandboxService
from rsi.tracing.collector import TraceCollector
from rsi.tracing.parsers import OpenCodeWiretapParser
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceFilter, TraceStore

PROMPT = "diagnose the link failure and reply with FINAL_JSON"
GOOD = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown", "confidence": 0.9}'
OUT = f"working...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


def _env() -> EnvSpec:
    return EnvSpec(
        name="lab-in012",
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
        task_id=f"fault/in012-{n:02d}",
        title=f"IN-012 task {n}",
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


class MockJudge:
    """Fixed-verdict judge recording judged task ids."""

    def __init__(self) -> None:
        self.judged_task_ids: list[str] = []

    async def judge_run(self, task, result, events, lab, sandbox, permission, **kw):
        tid = getattr(getattr(task, "task", None), "task_id", None) or "?"
        self.judged_task_ids.append(tid)
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
                diagnostic_steps=3,
                missed_check=0,
                false_positive=0,
            ),
            path_quality=PathQuality(verdict="pass", skipped_critical_steps=[], reasoning=""),
            efficiency=Efficiency(tokens=120, tool_calls=2, wall_time_s=1.5),
            safety=SafetyVerdict(),
            missing=False,
        )


class SpySandbox(SandboxService):
    """Sandbox spy: counts build_lab and records restore/prepare call order.

    ``fail_restore_for`` holds task_ids whose post-judgment restore always
    raises EnvError (both L1 retries and the L2 reset+restore replay).
    """

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.build_count = 0
        self.calls: list[tuple[str, str]] = []
        self.fail_restore_for: set[str] = set()
        self._current_task = ""

    async def build_lab(self, env):
        self.build_count += 1
        self.calls.append(("build", env.name))
        return await super().build_lab(env)

    async def prepare(self, lab, task_id, actions):
        self._current_task = task_id
        self.calls.append(("prepare", task_id))
        return await super().prepare(lab, task_id, actions)

    async def restore_task(self, lab, applied):
        if self._current_task in self.fail_restore_for:
            self.calls.append(("restore-fail", self._current_task))
            raise EnvError(f"simulated restore failure for {self._current_task}")
        self.calls.append(("restore", self._current_task))
        return await super().restore_task(lab, applied)

    async def restore(self, lab, snap):
        # B-24: the L2 snapshot restore fails for the same tasks so the
        # env_failed degradation path is still exercised end-to-end.
        if self._current_task in self.fail_restore_for:
            raise EnvError(
                f"simulated L2 restore failure for {self._current_task}"
            )
        return await super().restore(lab, snap)


def _rig(tmp_path: Path, sandbox: SpySandbox, judge: MockJudge) -> BatchRunner:
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
        ckpt=CheckpointStore(),
        budget=BudgetController(
            stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
        ),
        event_bus=EventBus(),
        stats_agg=StatsAggregator(),
    )


class TestIN012EnvReuseAndRestore:
    @pytest.mark.asyncio
    async def test_build_once_restore_between_tasks(self, tmp_path: Path) -> None:
        """3 任务批：build_lab 仅 1 次；每任务判决后 restore、下一任务
        prepare 前环境干净（restore 先于 prepare）。"""
        sandbox = SpySandbox(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
        )
        judge = MockJudge()
        runner = _rig(tmp_path, sandbox, judge)
        tasks = [_task(1), _task(2), _task(3)]

        stats = await runner.run_batch("batch-in012a", _env(), tasks, "cfg-v1", "sut-img")

        assert stats.n == 3
        assert sandbox.build_count == 1, "lab must be built once per batch"
        # every task judged exactly once, in order
        assert judge.judged_task_ids == [t.task_id for t in tasks]
        # restore precedes the next prepare for tasks 2 and 3
        for tid in (tasks[1].task_id, tasks[2].task_id):
            prep = sandbox.calls.index(("prepare", tid))
            assert any(
                c[0] == "restore" and c[1] != tid and i < prep
                for i, c in enumerate(sandbox.calls)
            ), f"no restore recorded before prepare of {tid}"
        # per-task restores happened for tasks 1 and 2 (before their successor)
        restores = [c for c in sandbox.calls if c[0] == "restore"]
        assert len(restores) >= 2

    @pytest.mark.asyncio
    async def test_restore_failure_degrades_task_not_batch(self, tmp_path: Path) -> None:
        """任务 2 restore 持续失败（L1 重试 + L2 均失败）→ 该任务计
        env_failed（missing verdict），任务 3 仍正常执行。"""
        sandbox = SpySandbox(
            backends={"fake": FakeSandboxBackend()}, labs_dir=tmp_path / "labs"
        )
        sandbox.fail_restore_for = {"fault/in012-02"}
        judge = MockJudge()
        runner = _rig(tmp_path, sandbox, judge)
        tasks = [_task(1), _task(2), _task(3)]

        stats = await runner.run_batch("batch-in012b", _env(), tasks, "cfg-v1", "sut-img")

        # batch completed (not aborted/failed)
        assert runner.last_status == "finalized"
        # task 2 was judged but its restore failed -> env_failed in checkpoint
        state = await runner.ckpt.resume("batch-in012b")
        by_tid = {c["task_id"]: c["status"] for c in state["completed"]}
        assert by_tid[tasks[1].task_id] == "env_failed"
        assert by_tid[tasks[0].task_id] == "done"
        assert by_tid[tasks[2].task_id] == "done"
        # restore failure was attempted (L1 retries observed)
        fails = [c for c in sandbox.calls if c[0] == "restore-fail"]
        assert fails, "simulated restore failures were never hit"
        # task 3 still prepared and executed after the degraded task 2
        assert ("prepare", tasks[2].task_id) in sandbox.calls
        assert judge.judged_task_ids == [t.task_id for t in tasks]  # judged pre-restore


# ---------------------------------------------------------------- IN-014

_WIRETAP_ASSEMBLED = json.dumps(
    {
        "type": "assembled",
        "kind": "context",
        "at": "2026-11-05T10:00:00Z",
        "model": "qwen3-coder",
        "system": [{"id": "sys-1", "text": "You are a network diagnosis agent."}],
        "messages": [
            {"role": "user", "content": "Diagnose the BGP peer 10.0.0.2 outage."},
            {"role": "assistant", "content": "Checking interfaces first."},
        ],
        "tools": [{"name": "ssh_cli", "description": "run a device command"}],
    }
)

_WIRETAP_HTTP = json.dumps(
    {
        "type": "http",
        "kind": "primary",
        "at": "2026-11-05T10:00:01Z",
        "model": "qwen3-coder",
        "body": json.dumps(
            {
                "model": "qwen3-coder",
                "messages": [{"role": "user", "content": "peer 10.0.0.2 down?"}],
                "stream": True,
            }
        ),
    }
)


class TestIN014WiretapChain:
    def test_wire_frames_full_text_in_store(self) -> None:
        """OpenCode wiretap NDJSON -> parser -> collector.ingest -> store.query:
        llm_request 全文入库（system/messages 完整、http 原始 body 不截断）。"""
        collector = TraceCollector(SeqAllocator(), None)
        store = TraceStore(collector=collector)

        parser = OpenCodeWiretapParser()
        events = [
            parser.parse_line(line, "run-in014", "fault/in014-01", "lab-w", "sut-oc")
            for line in (_WIRETAP_ASSEMBLED, _WIRETAP_HTTP)
        ]
        assert all(e is not None for e in events)
        collector.ingest(events)
        collector.finalize("run-in014")

        rows, _cursor = store.query(
            TraceFilter(run_ids=["run-in014"]), cursor=None, limit=50
        )
        assert len(rows) >= 2
        assert all(r.event_type == EventType.LLM_REQUEST for r in rows)
        # seq assigned by the collector and strictly monotonic per run
        seqs = [r.seq for r in rows]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        assembled = next(r for r in rows if "system" in r.data)
        assert assembled.data["system"] == ["You are a network diagnosis agent."]
        assert [m["role"] for m in assembled.data["messages"]] == ["user", "assistant"]
        assert "BGP peer 10.0.0.2" in assembled.data["messages"][0]["content"]
        assert assembled.data["tools"][0]["name"] == "ssh_cli"
        # estimated input tokens present (frame carries no usage)
        assert assembled.data["usage"]["input_tokens"] > 0
        assert assembled.data["usage"]["estimated"] is True

        http_row = next(r for r in rows if "body" in r.data)
        assert "peer 10.0.0.2 down?" in http_row.data["body"]  # un-truncated
        assert http_row.data["protocol"] == "chat_completions"

    def test_filtered_kind_and_garbage_yield_no_event(self) -> None:
        parser = OpenCodeWiretapParser()
        assert (
            parser.parse_line(
                json.dumps({"type": "assembled", "kind": "compaction"}),
                "r", "t", "l", "s",
            )
            is None
        )
        assert parser.parse_line("not-json{{", "r", "t", "l", "s") is None
