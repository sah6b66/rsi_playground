"""Audit4 G4 fixes: F1 token pipeline, F2 body-bound prompt hash, F3 vars
validation, judge temperature-0 guard + salvage parsing (T1-c)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from rsi.core.enums import EventType
from rsi.core.errors import ConfigError
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
)
from rsi.judge.aggregate import VerdictAggregator
from rsi.judge.antihack import AntiHackDetector
from rsi.judge.api import JudgeService
from rsi.judge.deterministic import DeterministicJudge
from rsi.judge.llm_judge import LLMJudge
from rsi.judge.safety import SafetyScanner
from rsi.llm.client import LLMClient
from rsi.llm.prompts import PromptLibrary
from rsi.tracing.collector import TraceCollector
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceFilter, TraceStore


# -- fixtures -----------------------------------------------------------------


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
                StateAssert(
                    device="R1",
                    cli="display interface GE0/0/1",
                    expect_contains=["DOWN"],
                )
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


def _good_handler():
    payloads = {
        "root_cause": json.dumps({"match": True, "confidence": 0.9}),
        "path_quality": json.dumps(
            {"verdict": "pass", "skipped_critical_steps": [], "reasoning": "ok"}
        ),
    }
    return lambda c, p, v: payloads[p]


def _service(handler=None) -> JudgeService:
    return JudgeService(
        det=DeterministicJudge(),
        safety=SafetyScanner(),
        llm=LLMJudge(LLMClient(handler=handler or _good_handler())),
        antihack=AntiHackDetector(),
        agg=VerdictAggregator(),
    )


def _event(seq: int, etype: EventType, data: dict) -> TraceEvent:
    return TraceEvent(
        run_id="r1",
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut",
        seq=seq,
        ts="2026-01-01T00:00:00.000+00:00",
        event_type=etype,
        data=data,
    )


class _Sandbox:
    def exec_readonly(self, lab, device, cli):
        return "GE0/0/1 current state : DOWN"


# -- F1.1: efficiency reads usage from any event type (wiretap LLM_REQUEST) ----


@pytest.mark.asyncio
async def test_efficiency_reads_usage_from_llm_request_frames() -> None:
    """Wiretap frames carry usage on LLM_REQUEST (parsers.py) — efficiency
    must count them (F1.1)."""
    events = [
        _event(
            0,
            EventType.LLM_REQUEST,
            {
                "system": ["s"],
                "messages": [],
                "usage": {"input_tokens": 60, "output_tokens": 40, "estimated": True},
            },
        )
    ]
    verdict = await _service().judge_run(
        _task(), TaskResult(task_complete=True), events, _lab(), _Sandbox(),
        permission="read-only",
    )
    assert verdict.efficiency.tokens == 100
    assert verdict.efficiency.estimated is True


def test_efficiency_matches_store_stats_tokens() -> None:
    """F1.3: JudgeService._efficiency and TraceStore.stats use the same
    usage-reading semantics — identical inputs give identical token counts."""
    from rsi.tracing.store import TraceStore  # local: store over collector

    events = [
        _event(
            0,
            EventType.LLM_REQUEST,
            {"usage": {"input_tokens": 60, "output_tokens": 40, "estimated": True}},
        ),
        _event(
            1,
            EventType.LLM_RESPONSE,
            {"usage": {"input_tokens": 10, "output_tokens": 5, "estimated": False}},
        ),
    ]
    seq = SeqAllocator()
    col = TraceCollector(seq, None)
    col.ingest(events)
    store = TraceStore(col)
    stats = store.stats(TraceFilter(run_ids=["r1"]))
    eff = JudgeService._efficiency(events)
    assert stats["tokens"] == eff.tokens == 115


# -- F1.2: wiretap ndjson tail wiring into the collector -----------------------


class _ChannelsCarrier:
    """Fake carrier facade exposing trace_channels like HostService."""

    def __init__(self, paths: list[str]) -> None:
        self.channels = SimpleNamespace(kind="ndjson", paths=paths)

    def trace_channels(self, instance) -> object:
        return self.channels


def test_ingest_trace_channels_tails_wiretap_file(tmp_path: Path) -> None:
    """A wiretap ndjson file with 2 frames is tailed into the collector and
    queryable through the TraceStore (F1.2)."""
    from rsi.carrier.adapters.fake import TraceChannels
    from rsi.orchestrator.batch import BatchRunner, image_id_of

    wiretap = tmp_path / "wiretap.ndjson"
    wiretap.write_text(
        json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "2026-01-01T00:00:00Z",
                "system": [{"text": "sys prompt"}],
                "messages": [{"content": "hello world"}],
                "model": "claude-x",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "http",
                "kind": "primary",
                "at": "2026-01-01T00:00:01Z",
                "body": json.dumps({"messages": [{"role": "u", "content": "q"}]}),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    seq = SeqAllocator()
    col = TraceCollector(seq, None)
    store = TraceStore(col)
    runner = SimpleNamespace(
        carrier=_ChannelsCarrier([str(wiretap), str(tmp_path / "missing.ndjson")]),
        collector=col,
    )
    task = _task().task
    lab = _lab()
    extra = BatchRunner._ingest_trace_channels(
        runner, "run-9", task, lab, SimpleNamespace(instance_id="i", run_id="run-9")
    )
    assert len(extra) == 2  # both frames ingested; missing path skipped quietly
    found = store.query(TraceFilter(run_ids=["run-9"]), None, 50)[0]
    assert len(found) == 2
    llm = [e for e in found if e.event_type == EventType.LLM_REQUEST]
    assert len(llm) == 2
    # estimated usage from the assembled frame flows into stats (H8)
    stats = store.stats(TraceFilter(run_ids=["run-9"]))
    assert stats["tokens"] > 0
    assert image_id_of(task) == "sut-1"


def test_ingest_trace_channels_quiet_when_no_files(tmp_path: Path) -> None:
    from rsi.orchestrator.batch import BatchRunner

    col = TraceCollector(SeqAllocator(), None)
    runner = SimpleNamespace(carrier=_ChannelsCarrier([]), collector=col)
    extra = BatchRunner._ingest_trace_channels(
        runner, "run-x", _task().task, _lab(), SimpleNamespace()
    )
    assert extra == []


# -- F2: prompt hash includes the template body --------------------------------


def test_hash_of_includes_body() -> None:
    lib_a = PromptLibrary({"judge/root_cause.v1": "judge this: {expected}"})
    lib_b = PromptLibrary({"judge/root_cause.v1": "judge this now: {expected}"})
    assert (
        lib_a.hash_of("judge/root_cause", "mock", 0.0)
        != lib_b.hash_of("judge/root_cause", "mock", 0.0)
    )
    # same body -> stable
    assert lib_a.hash_of("judge/root_cause", "mock", 0.0) == PromptLibrary(
        {"judge/root_cause.v1": "judge this: {expected}"}
    ).hash_of("judge/root_cause", "mock", 0.0)


@pytest.mark.asyncio
async def test_cassette_replay_misses_after_body_change() -> None:
    """A response recorded under the old body must not replay for the new
    body even with identical vars (F2)."""
    lib_a = PromptLibrary({"judge/score.v1": "score: {run_id}"})
    lib_b = PromptLibrary({"judge/score.v1": "score v2: {run_id}"})
    calls: list[str] = []

    def handler(component, prompt_name, vars):
        calls.append(vars["prompt"])
        return "out"

    client = LLMClient(handler, prompt_library=lib_a, model="mock")
    await client.complete("judge", "score", {"run_id": "r1"})
    assert len(calls) == 1
    # same library -> in-memory cassette hit, handler not called again
    await client.complete("judge", "score", {"run_id": "r1"})
    assert len(calls) == 1
    # body changed (same version) -> new key -> handler must run again
    client.prompt_library = lib_b
    await client.complete("judge", "score", {"run_id": "r1"})
    assert len(calls) == 2
    assert calls[1] == "score v2: r1"


# -- F3: template vars contract validation --------------------------------------


def test_render_missing_var_raises_config_error() -> None:
    lib = PromptLibrary({"judge/score.v1": "score {present} {missing}"})
    with pytest.raises(ConfigError):
        lib.render("judge", "score", {"present": "x"})


def test_render_full_vars_ok() -> None:
    lib = PromptLibrary({"judge/score.v1": "score {present} {missing}"})
    out = lib.render("judge", "score", {"present": "x", "missing": "y"})
    assert out == "score x y"


@pytest.mark.asyncio
async def test_llm_judge_propagates_config_error() -> None:
    """A drifted template (extra placeholder) surfaces as ConfigError, not a
    silent indeterminate (F3 / DD-02 §4)."""
    lib = PromptLibrary({"judge/root_cause.v1": "judge {expected} {drifted}"})
    client = LLMClient(_good_handler(), prompt_library=lib)
    judge = LLMJudge(client)
    with pytest.raises(ConfigError):
        await judge.judge(_task(), TaskResult(task_complete=True), [])


# -- P0: judge temperature-0 guard ----------------------------------------------


class _Out(BaseModel):
    verdict: str
    confidence: float


@pytest.mark.asyncio
async def test_judge_calls_forced_to_temperature_zero() -> None:
    seen: dict[str, float] = {}

    def handler(component, prompt_name, vars, *, temperature):
        seen[component] = temperature
        return '{"verdict": "pass", "confidence": 0.9}'

    client = LLMClient(handler, temperature=0.7)
    await client.complete("judge", "classify", {"x": "1"}, schema=_Out)
    await client.complete("critic", "classify", {"x": "1"}, schema=_Out)
    assert seen["judge"] == 0.0  # forced (ADR-23)
    assert seen["critic"] == 0.7  # other components untouched


def test_prompt_hash_uses_effective_temperature() -> None:
    client = LLMClient(lambda c, p, v: "x", temperature=0.7)
    judge_hash = client.prompt_hash("judge", "classify")
    cold = LLMClient(lambda c, p, v: "x", temperature=0.0)
    assert judge_hash == cold.prompt_hash("judge", "classify")


# -- P0: salvage parsing of fenced / embedded JSON ------------------------------


@pytest.mark.asyncio
async def test_salvage_markdown_fenced_json() -> None:
    def handler(component, prompt_name, vars):
        return (
            "Here you go:\n```json\n"
            '{"verdict": "pass", "confidence": 0.9}\n```\nthanks'
        )

    client = LLMClient(handler)
    resp = await client.complete("judge", "classify", {}, schema=_Out)
    assert isinstance(resp.parsed, _Out)
    assert resp.parsed.verdict == "pass"


@pytest.mark.asyncio
async def test_salvage_leading_prose_json_object() -> None:
    def handler(component, prompt_name, vars):
        return 'Answer: {"verdict": "suspect", "confidence": 0.4} — done.'

    client = LLMClient(handler)
    resp = await client.complete("judge", "classify", {}, schema=_Out)
    assert isinstance(resp.parsed, _Out)
    assert resp.parsed.verdict == "suspect"


@pytest.mark.asyncio
async def test_salvage_failure_still_invalid() -> None:
    client = LLMClient(lambda c, p, v: "no json here at all")
    from rsi.core.errors import LLMOutputInvalid

    with pytest.raises(LLMOutputInvalid):
        await client.complete("judge", "classify", {}, schema=_Out)
