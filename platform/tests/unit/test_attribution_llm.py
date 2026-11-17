"""B2-T2：LLM 精分类归因——逐簇细化、逐簇降级、cassette 回归."""

from __future__ import annotations

import json

import pytest

from rsi.optimizer.attribution import AttributionAnalyzer
from rsi.optimizer.attribution_llm import LLMRefinedAttributionAnalyzer


def _failed_run(rid="r1", commands=("display version",)):
    return {"run_id": rid, "events": [
        {"kind": "TOOL_CALL", "payload": {"command": c}} for c in commands
    ], "verdict": {"safety": {"violations": []},
                    "path_quality": {"skipped_critical_steps": []}},
        "scores": {"root_cause_accuracy": 0.0}, "result": {}}


def _schema_json(mode="细类:命令顺序错误", cls="A"):
    return json.dumps({
        "failure_mode": mode, "defect_class": cls,
        "evidence_refs": ["suts-ref:system-prompt.md:步骤3"],
        "rationale": "原文已有该指引但未执行", "directions": ["强化步骤级提示词"],
    }, ensure_ascii=False)


@pytest.mark.asyncio
async def test_refines_clusters_via_llm():
    async_ok = {"n": 0}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        assert component == "attribution" and prompt_name == "classify"
        assert "{cluster_samples}" not in str(vars)  # 已渲染替换
        async_ok["n"] += 1
        return _schema_json()

    from rsi.llm.client import LLMClient

    llm = LLMClient(handler=handler)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    report = await ana.analyze([_failed_run()], {"system-prompt.md": "..."}, [])
    assert report.source == "llm"
    assert async_ok["n"] >= 1
    assert report.clusters[0].rationale == "原文已有该指引但未执行"
    assert report.directions == ["强化步骤级提示词"]


@pytest.mark.asyncio
async def test_falls_back_to_rules_when_llm_fails():
    def bad(component, prompt_name, vars, *, temperature=0.0):
        raise RuntimeError("provider down")

    from rsi.llm.client import LLMClient

    llm = LLMClient(handler=bad)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    base = await AttributionAnalyzer().analyze([_failed_run()], {}, [])
    report = await ana.analyze([_failed_run()], {}, [])
    assert report.source == "rules"
    assert [c.failure_mode for c in report.clusters] == \
           [c.failure_mode for c in base.clusters]


@pytest.mark.asyncio
async def test_cassette_record_replay_roundtrip(tmp_path):
    from rsi.llm.cassette import CassetteStore
    from rsi.llm.client import LLMClient

    cs = CassetteStore(root=tmp_path, mode="record")
    llm = LLMClient(handler=lambda *a, **k: _schema_json(), cassette=cs)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    report = await ana.analyze([_failed_run()], {"f.md": "x"}, [])
    assert report.source == "llm"
    # replay：换一个会抛错的 handler，命中卡带仍得 llm 结果
    llm2 = LLMClient(
        handler=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")),
        cassette=CassetteStore(root=tmp_path, mode="replay"))
    ana2 = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm2)
    assert (await ana2.analyze([_failed_run()], {"f.md": "x"}, [])).source == "llm"


@pytest.mark.asyncio
async def test_opt_history_vars_capped():
    """B2-T8：opt_history json.dumps 后截断（同 _SUT_EVIDENCE_CAP 风格），
    超长近史不撑爆归因提示词，且截断不影响归因主流程."""
    from rsi.llm.client import LLMClient
    from rsi.optimizer.attribution_llm import _OPT_HISTORY_CAP

    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["vars"] = dict(vars)
        return _schema_json()

    llm = LLMClient(handler=handler)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    huge = [
        {"failure_mode": "x" * 200, "decision": "rejected", "target": "p.md"}
        for _ in range(100)
    ]
    report = await ana.analyze([_failed_run()], {"f.md": "x"}, huge)
    assert report.source == "llm"
    assert len(seen["vars"]["opt_history"]) <= _OPT_HISTORY_CAP


def _trace_event(run_id: str, command: str, seq: int = 0):
    """真实 TraceEvent（tracing collector 生产形态）——B2 终审 Fix1."""
    from datetime import UTC, datetime

    from rsi.core.enums import EventType
    from rsi.core.models import TraceEvent

    return TraceEvent(
        run_id=run_id,
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut-1",
        seq=seq,
        ts=datetime.now(UTC).isoformat(timespec="milliseconds"),
        event_type=EventType.TOOL_CALL,
        data={"args": {"command": command}},
    )


@pytest.mark.asyncio
async def test_traceevent_and_ducktyped_events_sample_commands():
    """B2 终审 Fix1：failed_runs 事件为 TraceEvent / duck-typed 对象形态
    不再 AttributeError 炸整轮 ATTRIBUTING，且命令进 cluster_samples."""
    from types import SimpleNamespace

    from rsi.core.enums import EventType
    from rsi.llm.client import LLMClient

    run = {
        "run_id": "r-obj",
        "events": [
            # 真实 TraceEvent（pydantic 对象）
            _trace_event("r-obj", "display version"),
            # duck-typed 事件对象（如垫片/适配层产出的轻量对象）
            SimpleNamespace(
                event_type=EventType.TOOL_CALL,
                data={"command": "display interface"},
            ),
        ],
    }
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["vars"] = dict(vars)
        return _schema_json()

    llm = LLMClient(handler=handler)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    report = await ana.analyze([run], {"f.md": "x"}, [])
    assert report.source == "llm"  # 对象形态事件不再炸整轮
    samples = seen["vars"]["cluster_samples"]
    assert "display version" in samples  # TraceEvent 命令被提取
    assert "display interface" in samples  # duck-typed 对象命令被提取


@pytest.mark.asyncio
async def test_collector_store_dict_shape_sample_commands():
    """B2 终审 Fix1：collector/store 形态 dict（type+data）命令被提取——
    此前只认测试桩 kind/payload 形态，命令恒空 → LLM 零证据静默精分类."""
    from rsi.llm.client import LLMClient

    run = {
        "run_id": "r-store",
        "events": [
            {"seq": 1, "type": "TOOL_CALL", "data": {"command": "display bgp peer"}},
            {"seq": 2, "type": "tool_result", "data": {"output": "ok"}},
        ],
    }
    seen = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["vars"] = dict(vars)
        return _schema_json()

    llm = LLMClient(handler=handler)
    ana = LLMRefinedAttributionAnalyzer(AttributionAnalyzer(), llm)
    report = await ana.analyze([run], {}, [])
    assert report.source == "llm"
    samples = seen["vars"]["cluster_samples"]
    assert samples == "r-store: display bgp peer"  # 命令进样本；tool_result 不混入
    # 大小写不敏感：kind 大写 TOOL_CALL 同样命中
    run_upper_kind = {
        "run_id": "r-kind",
        "events": [{"kind": "TOOL_CALL", "payload": {"command": "display clock"}}],
    }
    await ana.analyze([run_upper_kind], {}, [])
    assert "display clock" in seen["vars"]["cluster_samples"]
