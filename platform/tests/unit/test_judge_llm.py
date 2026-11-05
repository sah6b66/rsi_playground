"""TU for LLMJudge — DD-08 §5.2: two-prompt semantic judgment, indeterminate
on LLM failure."""

from __future__ import annotations

import json

import pytest

from rsi.core.enums import EventType
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    EvalTask,
    ExpectedAnswer,
    RootCauseExpectation,
    SshAccess,
    TaskRecord,
    TaskResult,
    TaskSpec,
    Topology,
    TraceEvent,
)
from rsi.judge.llm_judge import LLMJudge, SemanticScores
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
        ),
    )
    return EvalTask(env=env, task=rec)


def _events() -> list[TraceEvent]:
    return [
        TraceEvent(
            run_id="r1",
            task_id="fault/demo-01",
            lab_id="lab-1",
            sut_id="sut",
            seq=0,
            ts="2026-01-01T00:00:00.000+00:00",
            event_type=EventType.TOOL_CALL,
            data={"name": "ssh", "args": {"cli": "display interface GE0/0/1"}},
        )
    ]


def _client(handler) -> LLMClient:
    return LLMClient(handler=handler)


def _ok_handler(payloads: dict[str, str]):
    def handler(component, prompt_name, vars):
        return payloads[prompt_name]

    return handler


@pytest.mark.asyncio
async def test_valid_responses_combined() -> None:
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": json.dumps(
            {
                "verdict": "pass",
                "skipped_critical_steps": [],
                "reasoning": "all diagnostic steps present",
            }
        ),
    }
    judge = LLMJudge(_client(_ok_handler(payloads)))
    sem = await judge.judge(
        _task(), TaskResult(task_complete=True, root_cause="R1 GE0/0/1 admin down"), _events()
    )
    assert isinstance(sem, SemanticScores)
    assert sem.root_cause_match is True
    assert sem.root_cause_confidence == 0.9
    assert sem.path_quality.verdict == "pass"
    assert sem.consistency is True


@pytest.mark.asyncio
async def test_checklist_missed_and_false_positive_populated() -> None:
    captured: dict[str, dict] = {}
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": json.dumps(
            {
                "verdict": "pass",
                "skipped_critical_steps": [],
                "checklist_missed": ["check memory usage"],
                "checklist_false_positive": ["reset the device"],
                "reasoning": "one checklist item uncovered",
            }
        ),
    }

    def handler(component, prompt_name, vars):
        captured[prompt_name] = vars
        return payloads[prompt_name]

    judge = LLMJudge(_client(handler))
    sem = await judge.judge(_task(), TaskResult(task_complete=True), _events())
    assert sem.checklist_missed == ["check memory usage"]
    assert sem.checklist_false_positive == ["reset the device"]
    # checklist context threaded into the path_quality prompt
    assert "checklist_items" in captured["path_quality"]
    assert "sut_actions" in captured["path_quality"]


@pytest.mark.asyncio
async def test_root_cause_false_and_suspect_path() -> None:
    payloads = {
        "root_cause": json.dumps({"match": False, "confidence": 0.8}),
        "path_quality": json.dumps(
            {
                "verdict": "suspect",
                "skipped_critical_steps": ["ping 10.1.1.2"],
                "reasoning": "missing connectivity check",
            }
        ),
    }
    judge = LLMJudge(_client(_ok_handler(payloads)))
    sem = await judge.judge(_task(), TaskResult(task_complete=True, root_cause="wrong"), _events())
    assert sem.root_cause_match is False
    assert sem.path_quality.verdict == "suspect"
    assert sem.path_quality.skipped_critical_steps == ["ping 10.1.1.2"]
    # right-or-wrong conclusion aside, a suspect path with wrong conclusion
    # keeps consistency True (mismatch only when conclusion is right)
    assert sem.consistency is True


@pytest.mark.asyncio
async def test_conclusion_right_path_suspect_is_inconsistent() -> None:
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.95}),
        "path_quality": json.dumps(
            {"verdict": "suspect", "skipped_critical_steps": [], "reasoning": "guessed"}
        ),
    }
    judge = LLMJudge(_client(_ok_handler(payloads)))
    sem = await judge.judge(_task(), TaskResult(task_complete=True), _events())
    assert sem.consistency is False
    assert sem.consistency_reason


@pytest.mark.asyncio
async def test_invalid_output_raises_and_judge_returns_indeterminate() -> None:
    def bad_handler(component, prompt_name, vars):
        return "not json at all"

    judge = LLMJudge(_client(bad_handler))
    sem = await judge.judge(_task(), TaskResult(task_complete=True), _events())
    assert sem.root_cause_match is None
    assert sem.root_cause_confidence == 0.0


@pytest.mark.asyncio
async def test_handler_exception_returns_indeterminate() -> None:
    def boom(component, prompt_name, vars):
        raise RuntimeError("llm down")

    judge = LLMJudge(_client(boom))
    sem = await judge.judge(_task(), TaskResult(task_complete=True), _events())
    assert sem.root_cause_match is None


@pytest.mark.asyncio
async def test_path_quality_failure_returns_indeterminate() -> None:
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": "still not json",
    }

    # path_quality fails only when handler keeps returning invalid json;
    # root_cause succeeds first call.
    judge = LLMJudge(_client(_ok_handler(payloads)))
    sem = await judge.judge(_task(), TaskResult(task_complete=True), _events())
    assert sem.root_cause_match is None
