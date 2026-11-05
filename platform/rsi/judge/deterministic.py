"""DeterministicJudge — objective assertions layer (DD-08 §3/§4).

Evaluates ``expected.final_state`` StateAsserts through
``SandboxService.exec_readonly`` (read-only whitelist enforced there) and
counts ``critical_steps`` evidence hits against the run's trace events
(string containment in any value of ``event.data``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from rsi.core.models import CriticalStep, LabHandle, StateAssert, TaskResult
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import EvalTask, TraceEvent
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


def _event_texts(event: "TraceEvent") -> str:
    """Flatten an event's data into one searchable text blob."""
    try:
        return json.dumps(event.data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(event.data)


class DeterministicJudge:
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
            results.append(self._check_state(sa, lab, sandbox))

        hit_steps: list[str] = []
        for step in task.task.expected.critical_steps or []:
            if any(step.evidence in _event_texts(e) for e in events):
                hit_steps.append(step.evidence)

        scores = DetScores(
            final_state_results=results,
            critical_steps_hit=hit_steps,
            diagnostic_steps=len(hit_steps),
        )
        logger.debug(
            "evaluate.exit",
            state_asserts_passed=sum(1 for r in results if r.passed),
            critical_steps_hit=len(hit_steps),
        )
        return scores

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
