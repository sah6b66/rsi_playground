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
