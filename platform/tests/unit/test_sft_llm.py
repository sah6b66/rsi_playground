"""B2-7 FR6.9：SFT 报告 LLM 增强段——draft.v1 接线 + 失败回退."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsi.llm.client import LLMClient
from rsi.optimizer.sft import SftAssessor

_DISCLAIMER = "（本段由 LLM 生成，须人工复核后采用）"


def _clusters() -> list[dict]:
    return [
        {
            "failure_mode": "missed_critical_command",
            "defect_class": "B",
            "cluster_size": 30,
            "sample_run_ids": ["r1"],
            "evidence": [],
        },
        {
            "failure_mode": "wrong_vlan_rollback",
            "defect_class": "B",
            "cluster_size": 22,
            "sample_run_ids": ["r2"],
            "evidence": [],
        },
    ]


def _history() -> list[dict]:
    """两条 B 类缺陷各 5 轮无收益，且知识/skill 杠杆已尝试（准入全满足）."""
    hist = []
    for mode in ("missed_critical_command", "wrong_vlan_rollback"):
        for i in range(5):
            hist.append(
                {
                    "failure_mode": mode,
                    "decision": "reverted" if i % 2 == 0 else "advise_manual",
                    "proposal_type": (
                        "add_knowledge" if i % 2 == 0 else "modify_prompt"
                    ),
                }
            )
    return hist


@pytest.mark.asyncio
async def test_llm_section_after_admission_with_disclaimer(tmp_path):
    seen: dict = {}

    def handler(component, prompt_name, vars, *, temperature=0.0):
        seen["call"] = (component, prompt_name)
        seen["vars"] = dict(vars)
        return "两类 B 类缺陷均连续 5 轮无收益，建议停止自优化并转入知识工程。"

    assessor = SftAssessor(
        artifacts_dir=tmp_path, llm=LLMClient(handler=handler)
    )
    report = await assessor.assess(
        _clusters(), _history(), 60, exp_id="exp-llm", batch_id="b-1"
    )
    assert report is not None
    assert report.get("llm_analysis", "").startswith("两类")

    component, prompt_name = seen["call"]
    assert (component, prompt_name) == ("sft", "draft")
    vars = seen["vars"]
    assert {"defect_clusters", "opt_history"} <= set(vars)  # 模板两 vars 键齐
    assert (
        json.loads(vars["defect_clusters"])[0]["failure_mode"]
        == "missed_critical_command"
    )
    assert json.loads(vars["opt_history"])[0]["decision"] == "reverted"

    text = Path(report["report_path"]).read_text(encoding="utf-8")
    assert "## LLM 分析与建议" in text
    assert "两类 B 类缺陷均连续 5 轮无收益" in text  # handler 注入文本入段
    assert _DISCLAIMER in text  # 人工复核免责句
    # 插入位置：准入结论之后、失败模式明细之前
    assert (
        text.index("## 准入条件")
        < text.index("## LLM 分析与建议")
        < text.index("## 失败模式（无收益轮数）")
    )


@pytest.mark.asyncio
async def test_fallback_without_llm_section(tmp_path):
    # llm=None：完全老路径——报告与原模板一致，无 LLM 节
    legacy = SftAssessor(artifacts_dir=tmp_path)
    report = await legacy.assess(
        _clusters(), _history(), 60, exp_id="exp-none", batch_id="b-2"
    )
    assert report is not None
    assert "llm_analysis" not in report
    text = Path(report["report_path"]).read_text(encoding="utf-8")
    assert "LLM 分析与建议" not in text
    assert "准入条件" in text

    # LLM 失败：降级回模板文本（不含 LLM 节），不抛异常
    def bad(component, prompt_name, vars, *, temperature=0.0):
        raise RuntimeError("provider down")

    degraded = SftAssessor(
        artifacts_dir=tmp_path, llm=LLMClient(handler=bad)
    )
    report2 = await degraded.assess(
        _clusters(), _history(), 60, exp_id="exp-fb", batch_id="b-3"
    )
    assert report2 is not None
    assert "llm_analysis" not in report2
    text2 = Path(report2["report_path"]).read_text(encoding="utf-8")
    assert "LLM 分析与建议" not in text2
    assert "准入条件" in text2
