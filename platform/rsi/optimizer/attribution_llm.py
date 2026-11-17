"""FR6.2 P1：规则簇 → LLM 精分类（逐簇细化、逐簇降级，source 可审计）."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from rsi.core.enums import EventType
from rsi.core.models import AttributionCluster, AttributionReport
from rsi.log import get_logger
from rsi.optimizer.attribution import _event_fields

if TYPE_CHECKING:
    from rsi.llm.client import LLMClient
    from rsi.optimizer.attribution import AttributionAnalyzer

__all__ = ["LLMRefinedAttributionAnalyzer", "RefinedClusterOut"]

logger = get_logger(__name__)

_RUN_SUMMARY_CAP = 800
_SUT_EVIDENCE_CAP = 4000
# B2-T8：opt_history 序列化后同风格截断——超长近史不撑爆归因提示词
_OPT_HISTORY_CAP = 4000


def _run_sample_commands(run: dict) -> list[str]:
    """One failed run's executed commands, across all event shapes (B2 终审 Fix1).

    归一化复用规则层 ``_event_fields``：TraceEvent 对象、store/collector
    dict（``event_type``/``type`` + ``data``）、测试桩 dict（``kind`` +
    ``payload``）三种形态都能取到命令——此前只认测试桩形态，collector/
    store 形态命令恒空 → LLM 零证据静默精分类。
    """
    cmds: list[str] = []
    for ev in run.get("events") or []:
        etype, data = _event_fields(ev)
        if etype.lower() != str(EventType.TOOL_CALL):
            continue
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        cmd = (
            data.get("cli")
            or data.get("command")
            or args.get("cli")
            or args.get("command")
        )
        if isinstance(cmd, str) and cmd.strip():
            cmds.append(cmd)
    return cmds


class RefinedClusterOut(BaseModel):
    failure_mode: str
    defect_class: Literal["A", "B"]
    evidence_refs: list[str] = []
    rationale: str = ""
    directions: list[str] = []


class LLMRefinedAttributionAnalyzer:
    """Wrap the rule-layer analyzer with per-cluster LLM refinement (FR6.2).

    Each cluster is classified via the ``attribution/classify`` prompt;
    a failing cluster degrades to the rule-layer result (others unaffected).
    ``source`` flips to ``"llm"`` only when at least one cluster was refined.
    """

    def __init__(
        self,
        base: "AttributionAnalyzer",
        llm: "LLMClient",
        *,
        max_clusters: int = 5,
    ) -> None:
        self._base = base
        self._llm = llm
        self._max_clusters = max_clusters

    async def analyze(
        self,
        failed_runs: list[dict],
        sut_config: dict[str, str],
        opt_history: list[dict],
    ) -> AttributionReport:
        report = await self._base.analyze(failed_runs, sut_config, opt_history)
        refined_any = False
        directions: list[str] = []
        by_id = {r.get("run_id"): r for r in (failed_runs or [])}
        clusters: list[AttributionCluster] = []
        for idx, cluster in enumerate(report.clusters):
            if idx >= self._max_clusters:
                clusters.append(cluster)
                continue
            out = await self._classify_cluster(cluster, by_id, sut_config, opt_history)
            if out is None:
                clusters.append(cluster)
                continue
            clusters.append(
                AttributionCluster(
                    failure_mode=out.failure_mode or cluster.failure_mode,
                    defect_class=out.defect_class,
                    cluster_size=cluster.cluster_size,
                    sample_run_ids=list(cluster.sample_run_ids),
                    evidence=list(dict.fromkeys(
                        list(cluster.evidence) + list(out.evidence_refs))),
                    rationale=out.rationale or None,
                )
            )
            directions.extend(d for d in out.directions if d)
            refined_any = True
        if refined_any:
            report = AttributionReport(
                clusters=clusters,
                source="llm",
                directions=list(dict.fromkeys(directions)),
            )
        return report

    async def _classify_cluster(
        self,
        cluster: AttributionCluster,
        by_id: dict,
        sut_config: dict[str, str],
        opt_history: list[dict],
    ) -> RefinedClusterOut | None:
        """Call the LLM for one cluster; None means degrade to the rule result."""
        try:
            # B2 终审 Fix1：样本构建在 try 降级圈内——单簇证据组装异常
            # （事件形态/缺失字段）只丢弃该簇的 LLM 细化（保留规则结果），
            # 不再在 try 外炸掉整轮 ATTRIBUTING。
            samples: list[str] = []
            for rid in cluster.sample_run_ids[:5]:
                run = by_id.get(rid, {})
                cmds = _run_sample_commands(run)
                samples.append(f"{rid}: {'; '.join(cmds)}"[:_RUN_SUMMARY_CAP])
            if not samples:
                return None
            sut_ev = "\n".join(
                f"{k}: {str(v)[:400]}" for k, v in (sut_config or {}).items()
            )[:_SUT_EVIDENCE_CAP]
            vars = {
                "cluster_samples": "\n".join(samples),
                "failure_modes": cluster.failure_mode,
                "opt_history": json.dumps(
                    opt_history or [], ensure_ascii=False
                )[:_OPT_HISTORY_CAP],
                "sut_evidence": sut_ev or "（无配置原文）",
            }
            resp = await self._llm.complete(
                "attribution", "classify", vars, schema=RefinedClusterOut
            )
        except Exception as exc:  # noqa: BLE001 - 逐簇降级
            logger.warning(
                "attribution.llm_refine_failed",
                mode=cluster.failure_mode,
                error=str(exc),
            )
            return None
        parsed = resp.parsed
        assert isinstance(parsed, RefinedClusterOut)  # schema-validated by client
        return parsed
