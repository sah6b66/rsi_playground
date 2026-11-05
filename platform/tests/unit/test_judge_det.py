"""TU for DeterministicJudge — DD-08 §4: final_state contains/absent AND,
critical_steps hit count via tool_call event string match."""

from __future__ import annotations

import pytest

from rsi.core.enums import EventType
from rsi.core.models import (
    EnvSpec,
    EvalTask,
    LabHandle,
    SshAccess,
    StateAssert,
    TaskRecord,
    TaskResult,
    TaskSpec,
    CriticalStep,
    ExpectedAnswer,
)
from rsi.judge.deterministic import DeterministicJudge, StateAssertResult


def _lab() -> LabHandle:
    return LabHandle(
        lab_id="lab-1",
        host="localhost",
        backend="fake",
        endpoints={"R1": SshAccess(ip="127.0.0.1", username="u")},
    )


def _task(final_state, critical=None) -> EvalTask:
    from rsi.core.models import DeviceSpec, Topology

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
            final_state=final_state,
            critical_steps=critical,
        ),
    )
    return EvalTask(env=env, task=rec)


def _event(data: dict, seq: int = 0) -> object:
    from rsi.core.models import TraceEvent

    return TraceEvent(
        run_id="r1",
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut",
        seq=seq,
        ts="2026-01-01T00:00:00.000+00:00",
        event_type=EventType.TOOL_CALL,
        data=data,
    )


class _Sandbox:
    def __init__(self, outputs: dict[tuple[str, str], str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, str, str]] = []

    def exec_readonly(self, lab, device, cli):
        self.calls.append((lab.lab_id, device, cli))
        return self.outputs[(device, cli)]


@pytest.mark.asyncio
async def test_contains_and_absent_all_pass() -> None:
    sa = StateAssert(
        device="R1",
        cli="display ip routing-table",
        expect_contains=["10.1.1.0/24", "Direct"],
        expect_absent=["11.11.11.11"],
    )
    sandbox = _Sandbox(
        {("R1", "display ip routing-table"): "10.1.1.0/24 Direct 0 0\n GigabitEthernet0/0/1"}
    )
    det = DeterministicJudge()
    scores = await det.evaluate(_task([sa]), TaskResult(task_complete=True), _lab(), sandbox, [])
    assert len(scores.final_state_results) == 1
    r = scores.final_state_results[0]
    assert isinstance(r, StateAssertResult)
    assert r.passed is True
    assert r.matched == ["10.1.1.0/24", "Direct"]
    assert r.absent_ok is True
    assert sandbox.calls == [("lab-1", "R1", "display ip routing-table")]


@pytest.mark.asyncio
async def test_missing_contains_fails() -> None:
    sa = StateAssert(device="R1", cli="display bgp peer", expect_contains=["Established"])
    sandbox = _Sandbox({("R1", "display bgp peer"): "BGP peer is Idle"})
    det = DeterministicJudge()
    scores = await det.evaluate(_task([sa]), TaskResult(task_complete=True), _lab(), sandbox, [])
    assert scores.final_state_results[0].passed is False
    assert scores.final_state_results[0].matched == []


@pytest.mark.asyncio
async def test_absent_present_fails() -> None:
    sa = StateAssert(device="R1", cli="display interface brief", expect_absent=["down"])
    sandbox = _Sandbox({("R1", "display interface brief"): "GE0/0/1 is down"})
    det = DeterministicJudge()
    scores = await det.evaluate(_task([sa]), TaskResult(task_complete=True), _lab(), sandbox, [])
    assert scores.final_state_results[0].passed is False
    assert scores.final_state_results[0].absent_ok is False


@pytest.mark.asyncio
async def test_multiple_asserts_independent() -> None:
    sa1 = StateAssert(device="R1", cli="display a", expect_contains=["ok"])
    sa2 = StateAssert(device="R1", cli="display b", expect_contains=["ok"])
    sandbox = _Sandbox({("R1", "display a"): "ok", ("R1", "display b"): "bad"})
    det = DeterministicJudge()
    scores = await det.evaluate(
        _task([sa1, sa2]), TaskResult(task_complete=True), _lab(), sandbox, []
    )
    assert [r.passed for r in scores.final_state_results] == [True, False]


@pytest.mark.asyncio
async def test_critical_steps_hit_in_tool_calls() -> None:
    steps = [CriticalStep(evidence="display bgp peer"), CriticalStep(evidence="ping 10.1.1.2")]
    events = [
        _event({"name": "ssh", "args": {"cli": "display ip routing-table"}}, 0),
        _event({"name": "ssh", "args": {"cli": "display bgp peer 10.1.1.2 verbose"}}, 1),
    ]
    sandbox = _Sandbox({})
    det = DeterministicJudge()
    scores = await det.evaluate(
        _task([], critical=steps), TaskResult(task_complete=True), _lab(), sandbox, events
    )
    assert scores.critical_steps_hit == ["display bgp peer"]
    assert scores.diagnostic_steps == 1


@pytest.mark.asyncio
async def test_critical_steps_searches_any_event_data_string() -> None:
    steps = [CriticalStep(evidence="tracert 8.8.8.8")]
    events = [_event({"detail": "ran tracert 8.8.8.8 to check path"}, 0)]
    det = DeterministicJudge()
    scores = await det.evaluate(
        _task([], critical=steps), TaskResult(task_complete=True), _lab(), _Sandbox({}), events
    )
    assert scores.critical_steps_hit == ["tracert 8.8.8.8"]


@pytest.mark.asyncio
async def test_no_critical_steps_defaults_empty() -> None:
    det = DeterministicJudge()
    scores = await det.evaluate(_task([]), TaskResult(task_complete=True), _lab(), _Sandbox({}), [])
    assert scores.final_state_results == []
    assert scores.critical_steps_hit == []
    assert scores.diagnostic_steps == 0
