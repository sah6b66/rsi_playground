"""LLMJudge — semantic primary judgment layer (DD-08 §4/§5.2, ADR-23).

Two schema-validated LLM calls (``judge/root_cause`` + ``judge/path_quality``)
combined into :class:`SemanticScores`. Any LLM failure degrades to an
indeterminate result (``root_cause_match=None``) rather than raising — the
aggregator routes indeterminate verdicts to the human queue (DD-08 §5.3 rule 4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from rsi.core.models import PathQuality
from rsi.judge.summarizer import TraceSummarizer
from rsi.llm.client import LLMClient
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import EvalTask, TaskResult, TraceEvent

__all__ = ["LLMJudge", "PathQualityResult", "RootCauseMatchResult", "SemanticScores"]

logger = get_logger(__name__)


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

        ``config_version_id`` / ``batch_purpose`` (T2-5) are folded into the
        prompts as an evaluation-context fragment so the judge can
        distinguish baseline from candidate configurations.
        """
        logger.debug(
            "judge.entry",
            task=task.task.title,
            events=len(events),
            config_version_id=config_version_id,
        )
        ctx = self._ctx_fragment(config_version_id, batch_purpose)
        try:
            root = await self._judge_root_cause(task, result, ctx)
        except Exception as exc:  # noqa: BLE001 - degrade to indeterminate, not raise
            logger.debug("judge.indeterminate", stage="root_cause", exc_type=type(exc).__name__)
            return self._indeterminate("root_cause judgment unavailable")

        try:
            quality = await self._judge_path_quality(task, result, events, ctx)
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

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _ctx_fragment(
        config_version_id: str | None, batch_purpose: str | None
    ) -> str:
        """T2-5: evaluation-context fragment for the judge prompts."""
        parts: list[str] = []
        if config_version_id:
            parts.append(f"config_version={config_version_id}")
        if batch_purpose:
            parts.append(f"batch_purpose={batch_purpose}")
        return " ".join(parts)

    async def _judge_root_cause(
        self,
        task: "EvalTask",
        result: "TaskResult",
        ctx: str = "",
    ) -> RootCauseMatchResult:
        expected = task.task.expected.root_cause
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
                "eval_context": ctx,
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
        ctx: str = "",
    ) -> PathQualityResult:
        critical = task.task.expected.critical_steps or []
        checklist = task.task.expected.checklist or []
        resp = await self._llm.complete(
            "judge",
            "path_quality",
            vars={
                "steps_summary": self._summarizer.summarize(events),
                "eval_context": ctx,
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
