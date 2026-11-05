"""TU for judge Medium fixes — M4 TraceSummarizer + M5 Judgment persistence."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, Session, select

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.judge.api import JudgeService
from rsi.judge.summarizer import TraceSummarizer

# -- shared fixtures (construction copied from tests/unit/test_judge_api.py) --


def _ev(seq: int, etype, data) -> TraceEvent:
    return TraceEvent(
        run_id="run-m45",
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut",
        seq=seq,
        ts="2026-01-01T00:00:00.000+00:00",
        event_type=etype,
        data=data,
    )


def _task():
    from tests.unit.test_judge_api import _lab, _task

    return _task(), _lab()


def _service(handler, session_factory=None) -> JudgeService:
    from rsi.judge.aggregate import VerdictAggregator
    from rsi.judge.antihack import AntiHackDetector
    from rsi.judge.deterministic import DeterministicJudge
    from rsi.judge.llm_judge import LLMJudge
    from rsi.judge.safety import SafetyScanner
    from rsi.llm.client import LLMClient

    return JudgeService(
        det=DeterministicJudge(),
        safety=SafetyScanner(),
        llm=LLMJudge(LLMClient(handler=handler)),
        antihack=AntiHackDetector(),
        agg=VerdictAggregator(),
        session_factory=session_factory,
    )


def _good_handler():
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": json.dumps(
            {"verdict": "pass", "skipped_critical_steps": [], "reasoning": "ok"}
        ),
    }
    return lambda c, p, v: payloads[p]


class _Sandbox:
    def exec_readonly(self, lab, device, cli):
        return "GE0/0/1 current state : DOWN"


# -- M4: TraceSummarizer ------------------------------------------------------


def test_summarize_tool_call_compacts_args() -> None:
    long_args = {"payload": "x" * 500}
    text = TraceSummarizer().summarize([_ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": long_args})])
    assert "tool_call" in text
    assert "ssh" in text
    assert "x" * 400 not in text  # args truncated near 200 chars


def test_summarize_llm_request_system_truncated() -> None:
    text = TraceSummarizer().summarize(
        [
            _ev(
                1,
                EventType.LLM_REQUEST,
                {"system": "s" * 500, "messages": [{"role": "user"}, {"role": "user"}]},
            )
        ]
    )
    assert "llm_request" in text
    assert "messages=2" in text
    assert "s" * 300 not in text  # system truncated near 200 chars


def test_summarize_llm_response_keeps_usage() -> None:
    text = TraceSummarizer().summarize(
        [_ev(2, EventType.LLM_RESPONSE, {"finish_reason": "stop", "usage": {"input_tokens": 10}})]
    )
    assert "llm_response" in text
    assert "finish_reason=stop" in text
    assert "input_tokens" in text


def test_summarize_budget_compresses_oldest() -> None:
    events = [
        _ev(i, EventType.TOOL_CALL, {"name": "t", "args": {"cli": f"cmd-{i}"}})
        for i in range(50)
    ]
    text = TraceSummarizer().summarize(events, budget_tokens=50)
    assert "older events compressed" in text
    # allow one extra line of slack over the budget estimate
    assert len(text) // 4 <= 50 + 60


def test_summarize_other_types_list_keys() -> None:
    text = TraceSummarizer().summarize([_ev(0, EventType.ERROR, {"code": 1, "msg": "boom"})])
    assert "error" in text and "code" in text and "msg" in text


# -- M4: LLMJudge injection ---------------------------------------------------


@pytest.mark.asyncio
async def test_llm_judge_uses_summarizer() -> None:
    from rsi.core.models import TaskResult
    from rsi.judge.llm_judge import LLMJudge
    from rsi.llm.client import LLMClient

    calls: list = []

    class Stub:
        def summarize(self, events, budget_tokens=8000):
            calls.append(events)
            return "[0] tool_call stub"

    judge = LLMJudge(LLMClient(handler=_good_handler()), summarizer=Stub())
    task, _ = _task()
    events = [_ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": {"cli": "x"}})]
    await judge.judge(task, TaskResult(task_complete=True), events)
    assert calls and calls[0] is events


# -- M5: persistence -----------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_run_persists_judgment(tmp_path) -> None:
    from sqlalchemy import create_engine
    from rsi.db.tables import Judgment

    engine = create_engine(f"sqlite:///{tmp_path}/m5.db")
    SQLModel.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session)

    task, lab = _task()
    svc = _service(_good_handler(), session_factory=factory)
    events = [_ev(0, EventType.TOOL_CALL, {"name": "ssh", "args": {"cli": "display interface"}})]
    from rsi.core.models import TaskResult

    verdict = await svc.judge_run(task, TaskResult(task_complete=True), events, lab, _Sandbox(), permission="read-only")
    with factory() as s:
        rows = s.exec(select(Judgment)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == "run-m45"
    assert json.loads(row.verdict_json)["scores"]["task_complete"] is True
    assert row.llm_json and row.det_json


@pytest.mark.asyncio
async def test_judge_run_persist_failure_does_not_raise() -> None:
    from rsi.core.models import TaskResult

    def boom():
        raise RuntimeError("db down")

    task, lab = _task()
    svc = _service(_good_handler(), session_factory=boom)
    verdict = await svc.judge_run(
        task,
        TaskResult(task_complete=True),
        [_ev(0, EventType.LLM_RESPONSE, {"usage": {}})],
        lab,
        _Sandbox(),
        permission="read-only",
    )
    assert verdict is not None


@pytest.mark.asyncio
async def test_judge_run_no_factory_skips_persist() -> None:
    from rsi.core.models import TaskResult, Verdict

    task, lab = _task()
    svc = _service(_good_handler())
    verdict = await svc.judge_run(
        task,
        TaskResult(task_complete=True),
        [_ev(0, EventType.LLM_RESPONSE, {"usage": {}})],
        lab,
        _Sandbox(),
        permission="read-only",
    )
    assert isinstance(verdict, Verdict)  # no factory -> no persistence, no error
