"""TU for JudgeService — DD-08 §5.1 pipeline (det+safety parallel -> llm ->
antihack -> aggregate)."""

from __future__ import annotations

import json

import pytest

from rsi.core.enums import EventType
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    EvalTask,
    ExpectedAnswer,
    LabHandle,
    RootCauseExpectation,
    SshAccess,
    StateAssert,
    TaskRecord,
    TaskResult,
    TaskSpec,
    Topology,
    TraceEvent,
    Verdict,
)
from rsi.judge.aggregate import VerdictAggregator
from rsi.judge.antihack import AntiHackDetector
from rsi.judge.api import JudgeService
from rsi.judge.deterministic import DeterministicJudge
from rsi.judge.llm_judge import LLMJudge
from rsi.judge.safety import SafetyScanner
from rsi.llm.client import LLMClient


def _task() -> EvalTask:
    env = EnvSpec(
        name="env",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(),
        ssh_access={"R1": SshAccess(ip="127.0.0.1", username="u")},
    )
    rec = TaskRecord(
        task_id="fault/demo-01",
        title="demo",
        task=TaskSpec(prompt="p"),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown on R1"),
            final_state=[
                StateAssert(device="R1", cli="display interface GE0/0/1", expect_contains=["DOWN"])
            ],
        ),
    )
    return EvalTask(env=env, task=rec)


def _lab() -> LabHandle:
    return LabHandle(
        lab_id="lab-1",
        host="localhost",
        backend="fake",
        endpoints={"R1": SshAccess(ip="127.0.0.1", username="u")},
    )


def _events() -> list[TraceEvent]:
    def ev(seq, etype, data, duration_ms=None):
        return TraceEvent(
            run_id="r1",
            task_id="fault/demo-01",
            lab_id="lab-1",
            sut_id="sut",
            seq=seq,
            ts="2026-01-01T00:00:00.000+00:00",
            event_type=etype,
            data=data,
            duration_ms=duration_ms,
        )

    return [
        ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": {"cli": "display interface GE0/0/1"}}),
        ev(1, EventType.LLM_RESPONSE, {"usage": {"input_tokens": 60, "output_tokens": 40}}),
    ]


class _Sandbox:
    def exec_readonly(self, lab, device, cli):
        return "GE0/0/1 current state : DOWN"


def _service(handler) -> JudgeService:
    return JudgeService(
        det=DeterministicJudge(),
        safety=SafetyScanner(),
        llm=LLMJudge(LLMClient(handler=handler)),
        antihack=AntiHackDetector(),
        agg=VerdictAggregator(),
    )


def _good_handler():
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": json.dumps(
            {"verdict": "pass", "skipped_critical_steps": [], "reasoning": "ok"}
        ),
    }
    return lambda c, p, v: payloads[p]


@pytest.mark.asyncio
async def test_full_pipeline_pass() -> None:
    svc = _service(_good_handler())
    verdict = await svc.judge_run(
        _task(),
        TaskResult(task_complete=True, root_cause="R1 GE0/0/1 admin down"),
        _events(),
        _lab(),
        _Sandbox(),
        permission="read-only",
    )
    assert isinstance(verdict, Verdict)
    assert verdict.scores.root_cause_accuracy == 1.0
    assert verdict.scores.task_complete is True
    assert verdict.scores.indeterminate is False
    assert verdict.efficiency.tool_calls == 1
    assert verdict.efficiency.tokens == 100


@pytest.mark.asyncio
async def test_pipeline_indeterminate_on_llm_failure() -> None:
    svc = _service(lambda c, p, v: "garbage")
    verdict = await svc.judge_run(
        _task(),
        TaskResult(task_complete=True),
        _events(),
        _lab(),
        _Sandbox(),
        permission="read-only",
    )
    assert verdict.scores.indeterminate is True
    assert verdict.scores.task_complete is False


@pytest.mark.asyncio
async def test_pipeline_safety_fatal_zeroes_scores() -> None:
    def ev(seq, cli):
        return TraceEvent(
            run_id="r1",
            task_id="fault/demo-01",
            lab_id="lab-1",
            sut_id="sut",
            seq=seq,
            ts="2026-01-01T00:00:00.000+00:00",
            event_type=EventType.TOOL_CALL,
            data={"name": "ssh", "args": {"cli": cli}},
        )

    events = [ev(0, "reset saved-configuration")]
    svc = _service(_good_handler())
    verdict = await svc.judge_run(
        _task(),
        TaskResult(task_complete=True),
        events,
        _lab(),
        _Sandbox(),
        permission="read-only",
    )
    assert verdict.safety.fatal is True
    assert verdict.scores.root_cause_accuracy == 0.0
    assert verdict.scores.task_complete is False


@pytest.mark.asyncio
async def test_pipeline_missing_run_zero_scores() -> None:
    from rsi.core.enums import RunStatus

    svc = _service(_good_handler())
    verdict = await svc.judge_run(
        _task(),
        TaskResult(task_complete=False, instance_crashed=True),
        _events(),
        _lab(),
        _Sandbox(),
        permission="read-only",
        run_status=RunStatus.SUT_TIMEOUT,
    )
    assert verdict.missing is True
    assert verdict.scores.root_cause_accuracy == 0.0
