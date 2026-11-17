"""SftAssessor — DD-11 §5.3 SFT/DPO need assessment.

SFT/DPO is a *suggestion-only* escalation: the assessor never executes
training, it returns a report when all three admission criteria hold:

1. the same defect class has produced no gain for >= 5 optimization rounds;
2. at least 50 failed samples exist to build a training set;
3. the cheaper knowledge/skill levers have already been tried (and failed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rsi.log import get_logger

if TYPE_CHECKING:
    from rsi.llm.client import LLMClient

__all__ = ["SftAssessor"]

logger = get_logger(__name__)

NO_GAIN_THRESHOLD = 5  # default; override via settings optimizer.sft.no_gain_rounds
FAILED_SAMPLE_THRESHOLD = 50  # default; optimizer.sft.min_samples

_FAILED_DECISIONS = frozenset({"reverted", "advise_manual", "rejected", "revert"})
_KNOWLEDGE_LEVERS = frozenset(
    {"add_knowledge", "add_runbook", "add_fewshot_example", "modify_prompt"}
)
_KNOWLEDGE_TARGETS = frozenset({"knowledge", "skills"})


def _field(entry: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in entry and entry[name] is not None:
            return entry[name]
    return None


class SftAssessor:
    """Assess whether SFT/DPO should be *suggested* (never executed).

    Thresholds come from settings (``optimizer.sft.no_gain_rounds`` /
    ``optimizer.sft.min_samples``) — D-10; report files land under
    ``<artifacts_dir>/<owner>/reports/sft-<exp_id>.md`` (DD-11 §7④).
    """

    def __init__(
        self,
        no_gain_rounds: int = NO_GAIN_THRESHOLD,
        min_samples: int = FAILED_SAMPLE_THRESHOLD,
        artifacts_dir: str | Path | None = None,
        llm: "LLMClient | None" = None,
    ) -> None:
        self.no_gain_rounds = int(no_gain_rounds)
        self.min_samples = int(min_samples)
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        # B2-7 FR6.9：可选 LLM 起草客户端；None = 完全老路径（离线形态）
        self._llm = llm

    async def assess(
        self,
        defect_b_clusters: list[dict[str, Any]],
        opt_history: list[dict[str, Any]],
        failed_sample_count: int,
        *,
        exp_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a suggestion report when all 3 admission criteria hold.

        With ``exp_id`` the report is also written to
        ``var/artifacts/<owner>/reports/sft-<exp_id>.md`` (owner = batch_id
        or exp_id) and the dict carries ``report_path``.
        """
        # Criterion 1: same defect class (failure mode) with no-gain rounds >= 5.
        no_gain_by_mode: dict[str, int] = {}
        for entry in opt_history:
            mode = _field(entry, "failure_mode")
            decision = str(_field(entry, "decision", "action") or "").lower()
            if mode is None:
                continue
            if decision in _FAILED_DECISIONS:
                no_gain_by_mode[str(mode)] = no_gain_by_mode.get(str(mode), 0) + 1

        cluster_modes = [
            str(_field(c, "failure_mode") or "") for c in defect_b_clusters
        ]
        qualified_modes = [
            m
            for m in cluster_modes
            if no_gain_by_mode.get(m, 0) >= self.no_gain_rounds
        ]
        criterion_1 = bool(qualified_modes)

        # Criterion 2: enough failed samples to train on.
        criterion_2 = failed_sample_count >= self.min_samples

        # Criterion 3: knowledge / skill levers already attempted.
        levers_tried: set[str] = set()
        for entry in opt_history:
            proposal = _field(entry, "proposal_type", "lever")
            target = _field(entry, "target")
            if proposal is not None and str(proposal) in _KNOWLEDGE_LEVERS:
                levers_tried.add(str(proposal))
            if target is not None and str(target) in _KNOWLEDGE_TARGETS:
                levers_tried.add(str(target))
        criterion_3 = bool(levers_tried)

        logger.debug(
            "assess.exit",
            criterion_1=criterion_1,
            criterion_2=criterion_2,
            criterion_3=criterion_3,
            failed_samples=failed_sample_count,
        )
        if not (criterion_1 and criterion_2 and criterion_3):
            return None

        report: dict[str, Any] = {
            "recommendation": "SFT/DPO",
            "only_suggest": True,  # never execute autonomously (DD-11 §5.3)
            "defect_class": "B",
            "failure_modes": sorted(set(qualified_modes)),
            "no_gain_rounds": {
                m: no_gain_by_mode.get(m, 0) for m in sorted(set(qualified_modes))
            },
            "failed_sample_count": failed_sample_count,
            "levers_tried": sorted(levers_tried),
            "criteria": {
                "same_defect_no_gain_rounds": criterion_1,
                "failed_samples_ge_50": criterion_2,
                "knowledge_skill_tried": criterion_3,
            },
        }
        if exp_id is not None:
            narrative = await self._draft_narrative(
                defect_b_clusters, opt_history, exp_id=exp_id
            )
            if narrative:
                report["llm_analysis"] = narrative
            path = self._write_report(
                report,
                exp_id=exp_id,
                batch_id=batch_id,
                llm_section=narrative or None,
            )
            if path is not None:
                report["report_path"] = str(path)
        return report

    async def _draft_narrative(
        self,
        defect_b_clusters: list[dict[str, Any]],
        opt_history: list[dict[str, Any]],
        *,
        exp_id: str,
    ) -> str:
        """FR6.9: LLM 起草报告叙述段（sft/draft.v1，schema=None 取原文）.

        任何失败降级为空串（报告回退现有模板文本），不阻断主流程。
        """
        if self._llm is None or not defect_b_clusters:
            return ""
        vars = {
            "defect_clusters": json.dumps(
                defect_b_clusters, ensure_ascii=False
            ),
            "opt_history": json.dumps(opt_history, ensure_ascii=False),
        }
        try:
            resp = await self._llm.complete("sft", "draft", vars, schema=None)
        except Exception as exc:  # noqa: BLE001 - 失败降级，不阻断评估
            logger.warning(
                "sft.llm_draft_failed", error=str(exc), exp_id=exp_id
            )
            return ""
        return (resp.content or "").strip()

    def _write_report(
        self,
        report: dict[str, Any],
        *,
        exp_id: str,
        batch_id: str | None,
        llm_section: str | None = None,
    ) -> Path | None:
        """Write ``sft-<exp_id>.md`` under <artifacts>/<owner>/reports/ (D-10)."""
        base = self.artifacts_dir or Path("var/artifacts")
        owner = batch_id or exp_id
        reports_dir = Path(base) / owner / "reports"
        try:
            reports_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("sft.report_write_failed", error=str(exc), exp_id=exp_id)
            return None
        path = reports_dir / f"sft-{exp_id}.md"
        lines = [
            f"# SFT/DPO 建议报告 — {exp_id}",
            "",
            "> 仅建议，平台不自动执行训练（DD-11 §5.3）。",
            "",
            "## 准入条件",
            "",
            f"- 同缺陷类连续无收益轮数 ≥ {self.no_gain_rounds}: "
            f"{'满足' if report['criteria']['same_defect_no_gain_rounds'] else '不满足'}",
            f"- 失败轨迹样本量 ≥ {self.min_samples}: "
            f"{'满足' if report['criteria']['failed_samples_ge_50'] else '不满足'}"
            f"（实际 {report['failed_sample_count']}）",
            f"- 知识/skill 已尝试: "
            f"{'满足' if report['criteria']['knowledge_skill_tried'] else '不满足'}"
            f"（{', '.join(report['levers_tried']) or '无'}）",
            "",
        ]
        if llm_section:
            # FR6.9：LLM 增强段紧跟准入结论，其后才是失败模式明细
            lines += [
                "## LLM 分析与建议",
                "",
                llm_section,
                "",
                "（本段由 LLM 生成，须人工复核后采用）",
                "",
            ]
        lines += ["## 失败模式（无收益轮数）", ""]
        for mode in report["failure_modes"]:
            lines.append(f"- {mode}: {report['no_gain_rounds'][mode]} 轮")
        lines += ["", "## 建议动作", "", "- 人工评估 SFT/DPO 数据构建与训练计划", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.debug("sft.report_written", path=str(path))
        return path
