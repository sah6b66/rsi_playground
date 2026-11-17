"""LLMJudge — semantic primary judgment layer (DD-08 §4/§5.2, ADR-23).

Two schema-validated LLM calls (``judge/root_cause`` + ``judge/path_quality``)
combined into :class:`SemanticScores`. Any LLM failure degrades to an
indeterminate result (``root_cause_match=None``) rather than raising — the
aggregator routes indeterminate verdicts to the human queue (DD-08 §5.3 rule 4).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from rsi.core.errors import ConfigError
from rsi.core.models import PairwiseReport, PathQuality
from rsi.judge.summarizer import TraceSummarizer
from rsi.llm.client import LLMClient
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import EvalTask, TaskResult, TraceEvent

__all__ = [
    "LLMJudge",
    "PairwiseOut",
    "PathQualityResult",
    "RootCauseMatchResult",
    "SemanticScores",
]

logger = get_logger(__name__)

# B2 终审 Fix2：pairwise 单侧摘要总量上限（仿 attribution 层 cap 常量风格）。
# raw-dict 事件路径没有 summarizer budget 兜底（runs 端点生产源），超长
# 轨迹逐行拼接会撑爆 pairwise 提示词——拼接后整体截断并标记。
_SUMMARY_TOTAL_CAP = 8000


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SemanticScores(_M):
    """LLMJudge output (DD-08 §3)."""

    root_cause_match: bool | None  # None = indeterminate (insufficient evidence)
    root_cause_confidence: float = Field(default=0.0, ge=0, le=1)
    path_quality: PathQuality
    checklist_missed: list[str] = []
    checklist_false_positive: list[str] = []
    consistency: bool = True
    consistency_reason: str = ""


class RootCauseMatchResult(_M):
    """judge/root_cause schema."""

    match: bool
    confidence: float = Field(ge=0, le=1)


class PathQualityResult(_M):
    """judge/path_quality schema."""

    verdict: str  # "pass" | "suspect" (validated when mapped to PathQuality)
    skipped_critical_steps: list[str] = []
    checklist_missed: list[str] = []  # expected checklist items not covered
    checklist_false_positive: list[str] = []  # SUT actions matching no checklist item
    reasoning: str


class PairwiseOut(_M):
    """judge/pairwise schema (FR7.2 pairwise 对比判决)."""

    winner: Literal["baseline", "candidate", "tie"]
    root_cause_dim: str
    path_quality_dim: str
    consistency_dim: str
    rationale: str
    hack_suspect: bool = False
    hack_reasons: list[str] = []


class LLMJudge:
    def __init__(
        self,
        llm_client: LLMClient,
        summarizer: TraceSummarizer | None = None,
    ) -> None:
        self._llm = llm_client
        self._summarizer = summarizer or TraceSummarizer()

    async def judge(
        self,
        task: "EvalTask",
        result: "TaskResult",
        events: "list[TraceEvent]",
        *,
        config_version_id: str | None = None,
        batch_purpose: str | None = None,
    ) -> SemanticScores:
        """Semantic judgment of one run.

        ``config_version_id`` / ``batch_purpose`` (T2-5/F3) are accepted for
        provenance only: the caller (JudgeService) persists them with the
        Judgment row. They are deliberately NOT folded into the LLM prompts —
        a blind judge must not see which arm (baseline/candidate) produced
        the run (audit3 N2).
        """
        logger.debug(
            "judge.entry",
            task=task.task.title,
            events=len(events),
            config_version_id=config_version_id,
        )
        try:
            root = await self._judge_root_cause(task, result)
        except ConfigError:
            # F3 (audit4, DD-02 §4): a template/code vars contract drift is a
            # CONFIGURATION error, not a judgment-quality problem — surface it
            # (ConfigError maps to 503 at the API layer) instead of silently
            # degrading to indeterminate.
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to indeterminate, not raise
            logger.debug("judge.indeterminate", stage="root_cause", exc_type=type(exc).__name__)
            return self._indeterminate("root_cause judgment unavailable")

        try:
            quality = await self._judge_path_quality(task, result, events)
        except ConfigError:
            raise  # F3: see root_cause note above
        except Exception as exc:  # noqa: BLE001
            logger.debug("judge.indeterminate", stage="path_quality", exc_type=type(exc).__name__)
            return self._indeterminate("path_quality judgment unavailable")

        match = root.match
        path = PathQuality(
            verdict=quality.verdict if quality.verdict in ("pass", "suspect") else "suspect",
            skipped_critical_steps=quality.skipped_critical_steps,
            reasoning=quality.reasoning,
        )
        # consistency: a right conclusion reached through a suspect path is a
        # lucky guess, not a pass (DD-08 §3 consistency semantics)
        consistent = not (match is True and path.verdict == "suspect")
        reason = (
            "conclusion correct and process sound"
            if consistent
            else "root cause matches but path is suspect (lucky guess)"
        )
        result_scores = SemanticScores(
            root_cause_match=match,
            root_cause_confidence=root.confidence,
            path_quality=path,
            checklist_missed=quality.checklist_missed,
            checklist_false_positive=quality.checklist_false_positive,
            consistency=consistent,
            consistency_reason=reason,
        )
        logger.debug(
            "judge.exit",
            root_cause_match=result_scores.root_cause_match,
            path_verdict=path.verdict,
            consistent=consistent,
        )
        return result_scores

    async def pairwise(
        self,
        task: Any,
        base: "tuple[Any, Any]",
        cand: "tuple[Any, Any]",
        *,
        antihack_signals: str = "",
    ) -> PairwiseReport:
        """FR7.2: pairwise comparison of two same-task runs (DD-08 pairwise).

        Side-band enhancement — does NOT touch the ADR-23 single-run primary
        judgment path (``judge``). ``base``/``cand`` are ``(result, events)``
        pairs; events may be TraceEvent objects or raw trace dicts (traces
        store / collector shape). ``task`` accepts an :class:`EvalTask`
        wrapper or a plain mapping (``task_id``/``title``/``expected``).

        ConfigError (template/vars contract drift) and LLMOutputInvalid
        propagate to the caller — the API layer maps both to 503.
        """
        title, expected = self._pairwise_task_fields(task)
        vars = {
            "task_title": title,
            "expected": expected,
            "baseline_summary": self._side_summary(base[0], base[1]),
            "candidate_summary": self._side_summary(cand[0], cand[1]),
            "antihack_signals": antihack_signals or "",
        }
        resp = await self._llm.complete("judge", "pairwise", vars, schema=PairwiseOut)
        assert resp.parsed is not None
        return PairwiseReport(**resp.parsed.model_dump())

    # -- internals ----------------------------------------------------------------

    async def _judge_root_cause(
        self,
        task: "EvalTask",
        result: "TaskResult",
    ) -> RootCauseMatchResult:
        expected = task.task.expected.root_cause
        # C-7: match_hints are highlighting/prefilter cues (never criteria);
        # forward them to the prompt so the judge can focus its comparison.
        hints = "\n".join(getattr(expected, "match_hints", None) or []) if expected else ""
        resp = await self._llm.complete(
            "judge",
            "root_cause",
            vars={
                "expected": expected.expected if expected else "",
                "actual": result.root_cause or "",
                "task_title": task.task.title,
                "confidence_of_sut": str(result.confidence)
                if result.confidence is not None
                else "",
                "match_hints": hints,
            },
            schema=RootCauseMatchResult,
        )
        assert resp.parsed is not None
        return resp.parsed

    async def _judge_path_quality(
        self,
        task: "EvalTask",
        result: "TaskResult",
        events: "list[TraceEvent]",
    ) -> PathQualityResult:
        critical = task.task.expected.critical_steps or []
        checklist = task.task.expected.checklist or []
        resp = await self._llm.complete(
            "judge",
            "path_quality",
            vars={
                "steps_summary": self._summarizer.summarize(events),
                "critical_steps": "\n".join(s.evidence for s in critical),
                "checklist_items": "\n".join(ci.item for ci in checklist),
                "sut_actions": "\n".join(a.action for a in result.actions),
                "task_complete": str(result.task_complete),
                "actions": "\n".join(a.action for a in result.actions),
            },
            schema=PathQualityResult,
        )
        assert resp.parsed is not None
        return resp.parsed

    @staticmethod
    def _indeterminate(reason: str) -> SemanticScores:
        return SemanticScores(
            root_cause_match=None,
            root_cause_confidence=0.0,
            path_quality=PathQuality(verdict="pass", reasoning="indeterminate"),
            consistency=False,
            consistency_reason=reason,
        )

    # -- pairwise internals (FR7.2) --------------------------------------------------

    @staticmethod
    def _pairwise_task_fields(task: Any) -> tuple[str, str]:
        """Normalize task input to ``(task_title, expected_text)``."""
        inner = getattr(task, "task", None)  # EvalTask wrapper
        if inner is not None:
            title = str(getattr(inner, "title", "") or "")
            expected = getattr(inner, "expected", None)
            expected_text = (
                expected.model_dump_json()
                if expected is not None and hasattr(expected, "model_dump_json")
                else ""
            )
            return title, expected_text
        if isinstance(task, Mapping):
            title = str(task.get("title") or task.get("task_id") or "")
            raw = task.get("expected")
            if raw is None or isinstance(raw, str):
                return title, str(raw or "")
            return title, json.dumps(raw, ensure_ascii=False, default=str)
        return "", ""

    def _side_summary(self, result: Any, events: Any) -> str:
        """One run's evidence summary: trace lines + final conclusion line.

        TraceEvent lists reuse the shared TraceSummarizer; raw dicts (traces
        store / collector / test rigs) get a command-sequence rendering so
        the judge still sees what was executed.
        """
        if events and hasattr(events[0], "event_type"):
            summary = self._summarizer.summarize(list(events))
        elif events:
            summary = "\n".join(
                _dict_event_line(i, e) for i, e in enumerate(events)
            )
        else:
            summary = ""
        conclusion = self._conclusion_of(result)
        if conclusion:
            summary = (
                f"{summary}\nfinal_answer={conclusion}"
                if summary
                else f"final_answer={conclusion}"
            )
        if len(summary) > _SUMMARY_TOTAL_CAP:
            summary = summary[:_SUMMARY_TOTAL_CAP] + "…(truncated)"
        return summary or "(no trace events)"

    @staticmethod
    def _conclusion_of(result: Any) -> str:
        if result is None:
            return ""
        root = getattr(result, "root_cause", None)
        if root:
            return str(root)
        if isinstance(result, Mapping):
            for key in ("root_cause", "answer", "final_answer"):
                value = result.get(key)
                if value:
                    return str(value)
        return ""


def _dict_event_line(index: int, e: Mapping) -> str:
    """One-line rendering of a raw trace dict (non-TraceEvent event).

    Covers both the rig shape ``{"kind", "payload": {...}}`` and the store /
    collector dump shape ``{"seq", "type", "data": {...}}`` — the command
    text must stay visible so the judge can compare executed steps.
    """
    kind = str(e.get("kind") or e.get("type") or e.get("event_type") or "event")
    payload = e.get("payload")
    if not isinstance(payload, Mapping):
        payload = e.get("data") if isinstance(e.get("data"), Mapping) else {}
    seq = e.get("seq", index)
    command = str(payload.get("command") or payload.get("cli") or "")
    if not command and isinstance(payload.get("args"), Mapping):
        command = str(
            payload["args"].get("cli") or payload["args"].get("command") or ""
        )
    if not command:
        command = str(payload.get("name") or payload.get("tool") or "")
    output = " ".join(str(payload.get("output") or "").split())[:240]
    parts = [f"[{seq}] {kind}"]
    if command:
        parts.append(f"command={command[:200]}")
    if output:
        parts.append(f"output={output}")
    if not command and not output:
        keys = ",".join(sorted(k for k in payload.keys() if isinstance(k, str)))
        parts.append(f"keys=[{keys}]")
    return " ".join(parts)
