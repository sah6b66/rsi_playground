"""JudgeService — judgment pipeline orchestration (DD-08 §5.1).

``judge_run`` runs DeterministicJudge and SafetyScanner in parallel
(asyncio.gather), then LLMJudge sequentially (LLM rate limits), then
AntiHackDetector, then VerdictAggregator. Persistence (judgments rows) lands
with the orchestrator slice; this class returns the Verdict only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from rsi.core.enums import EventType, RunStatus
from rsi.core.models import Efficiency

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import EvalTask, LabHandle, TaskResult, TraceEvent, Verdict
    from rsi.judge.aggregate import VerdictAggregator
    from rsi.judge.antihack import AntiHackDetector
    from rsi.judge.deterministic import DeterministicJudge
    from rsi.judge.llm_judge import LLMJudge
    from rsi.judge.safety import SafetyScanner

__all__ = ["JudgeService"]

Permission = Literal["read-only", "read-write"]

_MISSING_STATUSES = frozenset({RunStatus.SUT_TIMEOUT, RunStatus.ABORTED, RunStatus.ENV_FAILED})


class JudgeService:
    def __init__(
        self,
        det: "DeterministicJudge",
        safety: "SafetyScanner",
        llm: "LLMJudge",
        antihack: "AntiHackDetector",
        agg: "VerdictAggregator",
    ) -> None:
        self._det = det
        self._safety = safety
        self._llm = llm
        self._antihack = antihack
        self._agg = agg

    async def judge_run(
        self,
        task: "EvalTask",
        result: "TaskResult",
        events: "list[TraceEvent]",
        lab: "LabHandle",
        sandbox: object,
        permission: Permission,
        *,
        run_status: "RunStatus | str | None" = None,
        efficiency: Efficiency | None = None,
    ) -> "Verdict":
        commands = self._extract_commands(events)

        async def safety_scan() -> object:
            return await asyncio.to_thread(self._safety.scan, commands, permission)

        det_scores, safety_verdict = await asyncio.gather(
            self._det.evaluate(task, result, lab, sandbox, events),
            safety_scan(),
        )
        sem = await self._llm.judge(task, result, events)
        hack = self._antihack.detect(task, events, det_scores, sem, result=result, run_status=run_status)
        eff = efficiency if efficiency is not None else self._efficiency(events)
        missing = self._missing(result, run_status)
        return self._agg.aggregate(det_scores, safety_verdict, sem, hack, eff, missing)

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _extract_commands(events: "list[TraceEvent]") -> list[str]:
        commands: list[str] = []
        for e in events:
            if e.event_type != EventType.TOOL_CALL:
                continue
            data = e.data
            cli = data.get("cli") or data.get("command")
            if not cli and isinstance(data.get("args"), dict):
                cli = data["args"].get("cli") or data["args"].get("command")
            if isinstance(cli, str) and cli.strip():
                commands.append(cli)
        return commands

    @staticmethod
    def _efficiency(events: "list[TraceEvent]") -> Efficiency:
        tokens = 0
        tool_calls = 0
        wall_ms = 0
        estimated = False
        for e in events:
            if e.event_type == EventType.TOOL_CALL:
                tool_calls += 1
            elif e.event_type == EventType.LLM_RESPONSE:
                usage = e.data.get("usage") or {}
                if isinstance(usage, dict):
                    tokens += int(usage.get("input_tokens", 0)) + int(
                        usage.get("output_tokens", 0)
                    )
                    estimated = estimated or bool(usage.get("estimated", False))
                tokens += int(e.data.get("tokens", 0))
            if e.duration_ms:
                wall_ms += e.duration_ms
        return Efficiency(
            tokens=tokens,
            tool_calls=tool_calls,
            wall_time_s=round(wall_ms / 1000, 3),
            estimated=estimated,
        )

    @staticmethod
    def _missing(result: "TaskResult", run_status: "RunStatus | str | None") -> bool:
        if run_status is not None:
            try:
                status = RunStatus(run_status)
            except ValueError:
                status = None
            if status is not None:
                return status in _MISSING_STATUSES
        return bool(result.instance_crashed)
