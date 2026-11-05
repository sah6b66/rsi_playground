"""LLMJudge — semantic primary judgment layer (DD-08 §4/§5.2, ADR-23).

Two schema-validated LLM calls (``judge/root_cause`` + ``judge/path_quality``)
combined into :class:`SemanticScores`. Any LLM failure degrades to an
indeterminate result (``root_cause_match=None``) rather than raising — the
aggregator routes indeterminate verdicts to the human queue (DD-08 §5.3 rule 4).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from rsi.core.models import PathQuality
from rsi.llm.client import LLMClient

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import EvalTask, TaskResult, TraceEvent

__all__ = ["LLMJudge", "PathQualityResult", "RootCauseMatchResult", "SemanticScores"]


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
    reasoning: str


def _event_texts(events: "list[TraceEvent]") -> str:
    parts: list[str] = []
    for e in events:
        try:
            parts.append(json.dumps(e.data, ensure_ascii=False, default=str))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            parts.append(str(e.data))
    return "\n".join(parts)


class LLMJudge:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def judge(
        self,
        task: "EvalTask",
        result: "TaskResult",
        events: "list[TraceEvent]",
    ) -> SemanticScores:
        try:
            root = await self._judge_root_cause(task, result)
        except Exception:  # noqa: BLE001 - degrade to indeterminate, not raise
            return self._indeterminate("root_cause judgment unavailable")

        try:
            quality = await self._judge_path_quality(task, result, events)
        except Exception:  # noqa: BLE001
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
        return SemanticScores(
            root_cause_match=match,
            root_cause_confidence=root.confidence,
            path_quality=path,
            consistency=consistent,
            consistency_reason=reason,
        )

    # -- internals ----------------------------------------------------------------

    async def _judge_root_cause(
        self, task: "EvalTask", result: "TaskResult"
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
            },
            schema=RootCauseMatchResult,
        )
        assert resp.parsed is not None
        return resp.parsed

    async def _judge_path_quality(
        self, task: "EvalTask", result: "TaskResult", events: "list[TraceEvent]"
    ) -> PathQualityResult:
        critical = task.task.expected.critical_steps or []
        resp = await self._llm.complete(
            "judge",
            "path_quality",
            vars={
                "steps_summary": _event_texts(events)[:8000],
                "critical_steps": "\n".join(s.evidence for s in critical),
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
