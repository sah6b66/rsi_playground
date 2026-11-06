"""Verifier: smoke double-run comparison (DD-06 §4).

P0 offline variant: base/overlay TaskResults are supplied by the caller
(BatchRunner smoke callback over FakeCarrier), and overlay events are read
from the instrumented side channel.
"""

from __future__ import annotations

from pydantic import BaseModel

from rsi.core.models import TaskResult


class VerifyResult(BaseModel):
    passed: bool
    task_result_match: bool
    events_produced: bool
    detail: str = ""


class Verifier:
    def verify(
        base_result: TaskResult,
        overlay_result: TaskResult,
        overlay_events: list,
    ) -> VerifyResult:
        match = Verifier._equivalent(base_result, overlay_result)
        # B-12: only real injected-side tool traffic counts — the injector's
        # own "instrumentation_applied" bootstrap marker must not make this
        # criterion trivially true.
        events = any(
            e.get("event_type") in ("tool_call", "tool_result")
            for e in overlay_events
            if isinstance(e, dict)
        )
        passed = match and events
        detail = Verifier._detail(match, events)
        return VerifyResult(
            passed=passed, task_result_match=match, events_produced=events, detail=detail
        )

    @staticmethod
    def _equivalent(a: TaskResult, b: TaskResult) -> bool:
        return a.task_complete == b.task_complete and a.root_cause == b.root_cause

    @staticmethod
    def _detail(match: bool, events: bool) -> str:
        parts = []
        parts.append(
            "TaskResults equivalent" if match else "TaskResults differ (task_complete/root_cause mismatch)"
        )
        parts.append(
            "instrumented side produced events" if events else "no events produced on instrumented side"
        )
        if match and events:
            return "smoke verify passed: " + "; ".join(parts)
        return "smoke verify failed: " + "; ".join(parts)
