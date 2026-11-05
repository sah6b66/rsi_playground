"""TU for AntiHackDetector — DD-08 FR7.4: skipped critical steps, missing
trial, conclusion-process mismatch."""

from __future__ import annotations

from rsi.core.enums import EventType, RunStatus
from rsi.core.models import (
    CriticalStep,
    DeviceSpec,
    EnvSpec,
    EvalTask,
    ExpectedAnswer,
    PathQuality,
    SshAccess,
    TaskRecord,
    TaskResult,
    TaskSpec,
    Topology,
    TraceEvent,
)
from rsi.judge.antihack import AntiHackDetector, HackSignals
from rsi.judge.deterministic import DetScores
from rsi.judge.llm_judge import SemanticScores


def _task(critical: list[CriticalStep] | None = None) -> EvalTask:
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
        expected=ExpectedAnswer(critical_steps=critical),
    )
    return EvalTask(env=env, task=rec)


def _tool_event(cli: str, seq: int) -> TraceEvent:
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


def _sem(match: bool | None, verdict: str) -> SemanticScores:
    return SemanticScores(
        root_cause_match=match,
        root_cause_confidence=0.9 if match else 0.0,
        path_quality=PathQuality(verdict=verdict, reasoning="r"),
    )


def test_skipped_steps_not_found_in_tool_calls() -> None:
    steps = [CriticalStep(evidence="display bgp peer"), CriticalStep(evidence="ping 10.1.1.2")]
    events = [_tool_event("display bgp peer 1.1.1.1 verbose", 0), _tool_event("display version", 1)]
    sig = AntiHackDetector().detect(_task(steps), events, DetScores(), _sem(True, "pass"))
    assert isinstance(sig, HackSignals)
    assert sig.skipped_steps == ["ping 10.1.1.2"]
    assert sig.missing_trial is False
    assert sig.conclusion_process_mismatch is False


def test_skipped_steps_only_checks_tool_call_events() -> None:
    steps = [CriticalStep(evidence="tracert 8.8.8.8")]
    other = TraceEvent(
        run_id="r1",
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut",
        seq=0,
        ts="2026-01-01T00:00:00.000+00:00",
        event_type=EventType.LLM_RESPONSE,
        data={"content": "I would run tracert 8.8.8.8"},
    )
    sig = AntiHackDetector().detect(_task(steps), [other], DetScores(), _sem(True, "pass"))
    assert sig.skipped_steps == ["tracert 8.8.8.8"]


def test_clean_case_no_signals() -> None:
    steps = [CriticalStep(evidence="display bgp peer")]
    events = [_tool_event("display bgp peer", 0)]
    sig = AntiHackDetector().detect(_task(steps), events, DetScores(), _sem(True, "pass"))
    assert sig.skipped_steps == []
    assert sig.missing_trial is False
    assert sig.conclusion_process_mismatch is False


def test_missing_trial_from_run_status() -> None:
    sig = AntiHackDetector().detect(
        _task(),
        [],
        DetScores(),
        _sem(True, "pass"),
        run_status=RunStatus.SUT_TIMEOUT,
    )
    assert sig.missing_trial is True


def test_missing_trial_from_crashed_result() -> None:
    sig = AntiHackDetector().detect(
        _task(),
        [],
        DetScores(),
        _sem(True, "pass"),
        result=TaskResult(task_complete=False, instance_crashed=True),
    )
    assert sig.missing_trial is True


def test_conclusion_process_mismatch() -> None:
    sig = AntiHackDetector().detect(
        _task(),
        [_tool_event("display version", 0)],
        DetScores(),
        _sem(True, "suspect"),
    )
    assert sig.conclusion_process_mismatch is True


def test_wrong_conclusion_with_suspect_path_is_not_mismatch() -> None:
    sig = AntiHackDetector().detect(
        _task(),
        [],
        DetScores(),
        _sem(False, "suspect"),
    )
    assert sig.conclusion_process_mismatch is False


def test_indeterminate_conclusion_is_not_mismatch() -> None:
    sig = AntiHackDetector().detect(
        _task(),
        [],
        DetScores(),
        _sem(None, "suspect"),
    )
    assert sig.conclusion_process_mismatch is False
