"""DeterministicJudge — objective assertions layer (DD-08 §3/§4).

Evaluates ``expected.final_state`` StateAsserts through
``SandboxService.exec_readonly`` (read-only whitelist enforced there) and
counts ``critical_steps`` evidence hits against the run's trace events
(string containment in any value of ``event.data``).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from rsi.core.models import CriticalStep, LabHandle, StateAssert, TaskResult
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import CheckItem, EvalTask, TraceEvent
    from rsi.sandbox.service import SandboxService

__all__ = ["DetScores", "DeterministicJudge", "StateAssertResult"]

logger = get_logger(__name__)


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StateAssertResult(_M):
    """Result of one final_state assertion (DD-08 §3)."""

    device: str
    cli: str
    passed: bool
    matched: list[str]  # expect_contains items found in output
    absent_ok: bool  # True iff no expect_absent item appeared


class DetScores(_M):
    """DeterministicJudge output (DD-08 §3)."""

    final_state_results: list[StateAssertResult] = []
    critical_steps_hit: list[str] = []
    diagnostic_steps: int = 0
    checklist_missed: list[str] = []


def _event_texts(event: "TraceEvent") -> str:
    """Flatten an event's data into one searchable text blob."""
    try:
        return json.dumps(event.data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(event.data)


class DeterministicJudge:
    def __init__(self, timeout_s: float | None = None) -> None:
        # D-11: judge.det_timeout_s consumer — bounds each read-only assert
        # execution; None keeps the unbounded legacy behavior.
        self.timeout_s = timeout_s

    async def evaluate(
        self,
        task: "EvalTask",
        result: TaskResult,
        lab: LabHandle,
        sandbox: "SandboxService",
        events: "list[TraceEvent] | None" = None,
    ) -> DetScores:
        events = events or []
        logger.debug(
            "evaluate.entry",
            task=task.task.title,
            events=len(events),
            final_state_asserts=len(task.task.expected.final_state),
        )
        results: list[StateAssertResult] = []
        for sa in task.task.expected.final_state:
            results.append(
                await self._check_state_bounded(sa, lab, sandbox)
            )

        hit_steps: list[str] = []
        for step in task.task.expected.critical_steps or []:
            if any(step.evidence in _event_texts(e) for e in events):
                hit_steps.append(step.evidence)

        missed: list[str] = [
            ci.item
            for ci in task.task.expected.checklist or []
            if not self._checklist_hit(ci, events, result)
        ]

        scores = DetScores(
            final_state_results=results,
            critical_steps_hit=hit_steps,
            diagnostic_steps=len(hit_steps),
            checklist_missed=missed,
        )
        logger.debug(
            "evaluate.exit",
            state_asserts_passed=sum(1 for r in results if r.passed),
            critical_steps_hit=len(hit_steps),
            checklist_missed=len(missed),
        )
        return scores

    @staticmethod
    def _checklist_hit(
        ci: "CheckItem", events: "list[TraceEvent]", result: TaskResult
    ) -> bool:
        """A checklist item is a hit when its evidence_cli appears in any
        event data (same containment logic as critical_steps) or when its
        description fuzzily matches a reported SUT action."""
        if ci.evidence_cli and any(ci.evidence_cli in _event_texts(e) for e in events):
            return True
        item = ci.item.strip().lower()
        if item and any(item in a.action.strip().lower() for a in result.actions):
            return True
        return False

    async def _check_state_bounded(
        self, sa: StateAssert, lab: LabHandle, sandbox: "SandboxService"
    ) -> StateAssertResult:
        """_check_state with the det_timeout_s bound (D-11).

        exec_readonly is blocking; run it in a worker thread under
        asyncio.wait_for. A timeout degrades to a failed assertion (the
        assert counts as not passed) instead of hanging the judge.
        """
        if self.timeout_s is None:
            return self._check_state(sa, lab, sandbox)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._check_state, sa, lab, sandbox),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "det.assert_timeout", device=sa.device, timeout_s=self.timeout_s
            )
            return StateAssertResult(
                device=sa.device,
                cli=sa.cli,
                passed=False,
                matched=[],
                absent_ok=True,
            )

    def _check_state(
        self, sa: StateAssert, lab: LabHandle, sandbox: "SandboxService"
    ) -> StateAssertResult:
        output = sandbox.exec_readonly(lab, sa.device, sa.cli)
        matched = [s for s in sa.expect_contains if s in output]
        absent_ok = not any(s in output for s in sa.expect_absent)
        return StateAssertResult(
            device=sa.device,
            cli=sa.cli,
            passed=(len(matched) == len(sa.expect_contains)) and absent_ok,
            matched=matched,
            absent_ok=absent_ok,
        )
