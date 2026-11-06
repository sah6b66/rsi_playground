"""JudgeService — judgment pipeline orchestration (DD-08 §5.1).

``judge_run`` runs DeterministicJudge and SafetyScanner in parallel
(asyncio.gather), then LLMJudge sequentially (LLM rate limits), then
AntiHackDetector, then VerdictAggregator. If ``session_factory`` is provided,
the aggregated judgment (three-layer raw outputs + Verdict) is persisted to
the ``judgments`` table; persistence failures are logged and never block the
evaluation flow (M5).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal

from rsi.core.enums import EventType, RunStatus
from rsi.core.models import Efficiency
from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from sqlalchemy.orm import Session
    from rsi.core.models import EvalTask, LabHandle, TaskResult, TraceEvent, Verdict
    from rsi.db.tables import Judgment
    from rsi.judge.aggregate import VerdictAggregator
    from rsi.judge.antihack import AntiHackDetector
    from rsi.judge.deterministic import DetScores, DeterministicJudge
    from rsi.judge.llm_judge import LLMJudge, SemanticScores
    from rsi.judge.safety import SafetyScanner

__all__ = ["JudgeService"]

logger = get_logger(__name__)

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
        session_factory: 'Callable[["Session"]] | None' = None,
        prompt_hash_fn: Callable[[], str] | None = None,
    ) -> None:
        self._det = det
        self._safety = safety
        self._llm = llm
        self._antihack = antihack
        self._agg = agg
        self._session_factory = session_factory
        # C-1: fingerprint of the judge prompt actually used for this run
        # (sha256(prompt version + model + temperature), DD-08 §5.1/§5.4).
        self._prompt_hash_fn = prompt_hash_fn

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
        config_version_id: str | None = None,
        batch_purpose: str | None = None,
    ) -> "Verdict":
        """Judge one run.

        ``config_version_id`` / ``batch_purpose`` (T2-5/F3) are provenance
        only: they are persisted with the Judgment row (eval_context_json)
        and never reach the judge LLM prompts (audit3 N2 blind-eval fix).
        """
        logger.debug(
            "judge_run.entry",
            task=task.task.title,
            events=len(events),
            permission=permission,
            run_status=str(run_status) if run_status is not None else None,
            config_version_id=config_version_id,
            batch_purpose=batch_purpose,
        )
        commands = self._extract_commands(events)

        async def safety_scan() -> object:
            return await asyncio.to_thread(self._safety.scan, commands, permission)

        det_scores, safety_verdict = await asyncio.gather(
            self._det.evaluate(task, result, lab, sandbox, events),
            safety_scan(),
        )
        sem = await self._llm.judge(
            task,
            result,
            events,
            config_version_id=config_version_id,
            batch_purpose=batch_purpose,
        )
        sem = self._merge_checklist(sem, det_scores)
        hack = self._antihack.detect(task, events, det_scores, sem, result=result, run_status=run_status)
        eff = efficiency if efficiency is not None else self._efficiency(events)
        missing = self._missing(result, run_status)
        verdict = self._agg.aggregate(det_scores, safety_verdict, sem, hack, eff, missing)
        if not events:
            # C-7: an empty event list has no run identity — persisting would
            # write a run_id="" junk row, so skip (with a warning) instead.
            logger.warning("judge_run.skip_persist_no_events", task=task.task.title)
        else:
            self._persist_judgment(
                run_id=events[0].run_id,
                verdict=verdict,
                det_scores=det_scores,
                safety_verdict=safety_verdict,
                sem=sem,
                hack=hack,
                config_version_id=config_version_id,
                batch_purpose=batch_purpose,
            )
        logger.debug(
            "judge_run.exit",
            task_complete=verdict.scores.task_complete,
            missing=verdict.missing,
        )
        return verdict

    def _persist_judgment(
        self,
        run_id: str,
        verdict: "Verdict",
        det_scores: "DetScores",
        safety_verdict,
        sem: "SemanticScores",
        hack,
        *,
        config_version_id: str | None = None,
        batch_purpose: str | None = None,
    ) -> None:
        if self._session_factory is None:
            return
        try:
            import json as _json

            from rsi.db.tables import Judgment

            def _j(obj) -> str | None:
                return obj.model_dump_json() if obj is not None else None

            eval_ctx = _json.dumps(
                {
                    "config_version_id": config_version_id,
                    "batch_purpose": batch_purpose,
                },
                ensure_ascii=False,
            )
            row = Judgment(
                id=str(uuid.uuid4()),
                run_id=run_id,
                det_json=_j(det_scores),
                safety_json=_j(safety_verdict),
                llm_json=_j(sem),
                verdict_json=verdict.model_dump_json(),
                judge_prompt_hash=self._prompt_hash_fn() if self._prompt_hash_fn else None,
                eval_context_json=eval_ctx,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            with self._session_factory() as session:
                session.add(row)
                session.commit()
            logger.debug("judgment.persisted", run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - persistence must not block judging
            logger.warning(
                "judgment.persist_failed", run_id=run_id, exc_type=type(exc).__name__
            )

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _merge_checklist(sem: "SemanticScores", det: "DetScores") -> "SemanticScores":
        """Union deterministic evidence-based misses with LLM-judged misses so
        inspection-task scores reflect the task checklist (C1)."""
        if not det.checklist_missed:
            return sem
        missed = list(sem.checklist_missed)
        for item in det.checklist_missed:
            if item not in missed:
                missed.append(item)
        return sem.model_copy(update={"checklist_missed": missed})

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
