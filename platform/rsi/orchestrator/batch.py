"""BatchRunner — batch state machine and 9-step task pipeline (DD-12 §4.1/§5).

P0 composition root: SandboxService (lab lifecycle + prepare/restore),
HostService (SUT instances), TraceCollector (event ingest/finalize), a
JudgeService-compatible judge, CheckpointStore, BudgetController, EventBus
and StatsAggregator. Pure orchestration: no device/container IO of its own.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from rsi.core.enums import RunStatus
from rsi.core.errors import BudgetExceeded, EnvError
from rsi.core.models import (
    BatchStats,
    Efficiency,
    EvalTask,
    EventType,
    LabHandle,
    PathQuality,
    Scores,
    SafetyVerdict,
    TaskRecord,
    TaskResult,
    TraceEvent,
    Verdict,
)
from rsi.core.naming import new_id
from rsi.log import get_logger

__all__ = ["BatchRunner"]

logger = get_logger(__name__)

_PREPARE_ATTEMPTS = 3  # 1 initial + 2 retries (DD-12 §5.2 step 3)
_MAX_CONSECUTIVE_ENV_FAILURES = 3  # F1 circuit breaker (DD-12 §5.1)
_EST_TOKENS_PER_TASK = 50_000


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _env_failed_verdict() -> Verdict:
    """Zero-score verdict; env-failed runs stay in the denominator (缺试计0)."""
    return Verdict(
        scores=Scores(
            root_cause_accuracy=0.0,
            task_complete=False,
            diagnostic_steps=0,
            missed_check=0,
            false_positive=0,
        ),
        path_quality=PathQuality(
            verdict="suspect", skipped_critical_steps=[], reasoning="env_failed"
        ),
        efficiency=Efficiency(),
        safety=SafetyVerdict(),
        missing=True,
    )


class BatchRunner:
    def __init__(
        self,
        sandbox,
        carrier,
        collector,
        trace_store,
        judge,
        ckpt,
        budget,
        event_bus,
        stats_agg,
        *,
        settings=None,
        task_timeout_s: float | None = None,
        host: str = "main",
    ) -> None:
        self.sandbox = sandbox
        self.carrier = carrier
        self.collector = collector
        self.trace_store = trace_store
        self.judge = judge
        self.ckpt = ckpt
        self.budget = budget
        self.event_bus = event_bus
        self.stats_agg = stats_agg
        self.settings = settings
        if task_timeout_s is None:
            task_timeout_s = (
                float(settings.carrier.task_timeout_s)
                if settings is not None
                else 1800.0
            )
        self.task_timeout_s = task_timeout_s
        # settings wiring (H1): build retry limit + ready timeout from settings
        self.prepare_attempts = (
            settings.sandbox.build_retry + 1 if settings is not None else _PREPARE_ATTEMPTS
        )
        self.ready_timeout_s = (
            settings.sandbox.ready_timeout_s if settings is not None else 600
        )
        self.host = host
        self._envs: dict[str, Any] = {}
        # introspection for tests/ops: latest batch status and task->run_id map
        self.last_status: str = "created"
        self.last_run_ids: dict[str, str] = {}

    # -- batch loop (§5.1) ---------------------------------------------------

    async def run_batch(
        self,
        batch_id: str,
        env,
        tasks: list[TaskRecord],
        config_version_id: str,
        image_ref: str,
        permission: str = "read-only",
    ) -> BatchStats:
        logger.debug(
            "run_batch.entry",
            batch_id=batch_id,
            tasks=len(tasks),
            image_ref=image_ref,
            permission=permission,
        )
        # 0. resume from checkpoint (DD-12 §5.1): skip already-completed tasks
        state = await self.ckpt.resume(batch_id)
        completed: list[dict[str, str]] = []
        completed_task_ids: set[str] = set()
        if state is not None:
            prev = state.get("completed", [])
            completed = [dict(c) for c in prev]
            completed_task_ids = {
                c["task_id"] if isinstance(c, dict) else str(c) for c in prev
            }
            logger.info(
                "batch.resume",
                batch_id=batch_id,
                completed_count=len(completed_task_ids),
            )
        pending = [t for t in tasks if t.task_id not in completed_task_ids]

        # 1-2. build lab once per batch (or reuse the checkpointed one), snap0
        lab, snap0 = await self._build_or_reuse_lab(env, state)
        self._envs[batch_id] = env

        verdicts: list[Verdict] = []
        status = "finalized"
        consecutive_env_failures = 0
        self.last_status = "running"
        await self.event_bus.publish("batch", batch_id, "created", "running")

        try:
            for seq, task in enumerate(pending):
                # a. stop file -> abort
                if self.budget.check_stop_file():
                    status = "aborted"
                    logger.debug("run_batch.abort", reason="stop_file", seq=seq)
                    await self._publish_alert(
                        "warning", "STOP 熔断文件检测到，批次中止", scope=batch_id
                    )
                    break
                # b. budget -> abort
                try:
                    await self.budget.check_and_reserve(
                        batch_id, _EST_TOKENS_PER_TASK
                    )
                except BudgetExceeded:
                    status = "aborted"
                    logger.debug("run_batch.abort", reason="budget", seq=seq)
                    await self._publish_alert(
                        "critical", "预算超限，批次中止", scope=batch_id
                    )
                    break

                # c. run task (9-step pipeline)
                result, verdict = await self.run_task(
                    batch_id,
                    task,
                    lab,
                    snap0.snapshot_id,
                    config_version_id,
                    image_ref,
                    permission,
                    seq,
                )
                verdicts.append(verdict)
                run_status = (
                    RunStatus.ENV_FAILED if verdict.missing else RunStatus.DONE
                )
                completed.append(
                    {
                        "task_id": task.task_id,
                        "run_id": self.last_run_ids.get(task.task_id, ""),
                        "status": run_status.value,
                    }
                )

                # F1: consecutive env-failure circuit breaker
                if verdict.missing:
                    consecutive_env_failures += 1
                    if consecutive_env_failures >= _MAX_CONSECUTIVE_ENV_FAILURES:
                        status = "failed"
                        logger.debug(
                            "run_batch.circuit_breaker",
                            consecutive=consecutive_env_failures,
                            seq=seq,
                        )
                        await self._publish_alert(
                            "critical",
                            f"沙箱离线: 连续 {consecutive_env_failures} 个任务环境失败",
                            scope=batch_id,
                        )
                        break
                else:
                    consecutive_env_failures = 0

                # usage + checkpoint + event (steps d/e)
                await self.budget.record(
                    batch_id,
                    verdict.efficiency.tokens,
                    0.0,
                    verdict.efficiency.wall_time_s,
                )
                await self.ckpt.save(
                    batch_id,
                    {
                        "batch_id": batch_id,
                        "lab_id": lab.lab_id,
                        "snap0_id": snap0.snapshot_id,
                        "completed": completed,
                        "cursor_seq": seq + 1,
                    },
                )
                await self.event_bus.publish(
                    "run", completed[-1]["run_id"], "judging", run_status.value
                )
        finally:
            # 5. destroy lab (always, also on abort)
            await self.sandbox.destroy(lab)
            self._envs.pop(batch_id, None)

        self.last_status = status
        await self.event_bus.publish("batch", batch_id, "running", status)
        # 4. aggregate verdicts -> BatchStats
        stats = self.stats_agg.aggregate(verdicts)
        logger.debug(
            "run_batch.exit", batch_id=batch_id, status=status, n=stats.n
        )
        return stats

    # -- task pipeline (§5.2) --------------------------------------------------

    async def run_task(
        self,
        batch_id: str,
        task: TaskRecord,
        lab: LabHandle,
        snap0_id: str,
        config_version_id: str,
        image_ref: str,
        permission: str,
        seq: int,
    ) -> tuple[TaskResult, Verdict]:
        run_id = new_id("run")
        self.last_run_ids[task.task_id] = run_id
        logger.debug("run_task.entry", run_id=run_id, task_id=task.task_id, seq=seq)

        # 1. prepare (backup + exec_cli/call_api), retry <= 2, then env_failed
        applied = []
        prepared = False
        for _attempt in range(self.prepare_attempts):
            try:
                applied = await self.sandbox.prepare(lab, task.task_id, task.prepare)
                prepared = True
                break
            except EnvError as exc:
                logger.debug(
                    "run_task.prepare_retry", task_id=task.task_id, exc_type=type(exc).__name__
                )
                continue
        if not prepared:
            logger.debug("run_task.exit", run_id=run_id, status="env_failed")
            result = TaskResult(task_complete=False)
            return result, _env_failed_verdict()

        # 4. create SUT instance (P0: one instance per run)
        instance = self.carrier.create_instance(
            image_ref=image_ref,
            config_version_id=config_version_id,
            lab=lab,
            permission=permission,
            instrument=True,
            host=self.host,
            cred=None,
            run_id=run_id,
        )
        try:
            # 5. submit -> collect (timeout from config)
            handle = self.carrier.submit_task(instance, task.task)
            result = await asyncio.to_thread(
                self.carrier.collect, handle, self.task_timeout_s
            )

            # 6. collect trace events (P0: synthesized from the result actions)
            events = self._trace_events(run_id, task, lab, result)
            self.collector.ingest(events)
        finally:
            self.collector.finalize(run_id)

        # 7. judge
        env = self._envs.get(batch_id)
        eval_task = EvalTask(env=env, task=task) if env is not None else None
        verdict = await self.judge.judge_run(
            eval_task,
            result,
            events,
            lab,
            self.sandbox,
            permission,
            run_status=RunStatus.DONE,
        )

        # 8. task-level restore AFTER judgment (final-state asserts need the
        # fault state present, ADR-20 L1)
        await self.sandbox.restore_task(lab, applied)

        # 9. destroy instance
        self.carrier.destroy(instance)

        logger.debug(
            "run_task.exit",
            run_id=run_id,
            task_complete=result.task_complete,
            missing=verdict.missing,
        )
        return result, verdict

    # -- helpers ----------------------------------------------------------------

    async def _publish_alert(
        self, severity: str, message: str, scope: str | None = None
    ) -> None:
        """system.alert publisher (H2): no-op when the bus lacks publish_alert."""
        publish_alert = getattr(self.event_bus, "publish_alert", None)
        if publish_alert is not None:
            await publish_alert(severity, message, scope=scope)

    async def _build_or_reuse_lab(self, env, state: dict | None):
        """Build a fresh lab, or reuse the checkpointed one if still alive."""
        if state is not None and state.get("lab_id"):
            candidate = LabHandle(
                lab_id=state["lab_id"],
                host=self.host,
                backend=env.backend,
                endpoints=dict(env.ssh_access or {}),
            )
            try:
                alive = await self.sandbox.health_check(candidate)
            except Exception:
                alive = False  # unknown lab / backend gone (fresh process)
            if alive:
                logger.info(
                    "batch.lab_reused", lab_id=candidate.lab_id, batch=state.get("batch_id")
                )
                snap0_id = state.get("snap0_id")
                for snap in self.sandbox.snapshots(candidate.lab_id):
                    if snap.snapshot_id == snap0_id:
                        return candidate, snap
                return candidate, await self.sandbox.snapshot(candidate, "snap0")
            logger.info("batch.lab_dead", lab_id=state.get("lab_id"))
        lab = await self.sandbox.build_lab(env)
        await self.sandbox.wait_ready(lab, self.ready_timeout_s)
        return lab, await self.sandbox.snapshot(lab, "snap0")

    @staticmethod
    def _trace_events(
        run_id: str,
        task: TaskRecord,
        lab: LabHandle,
        result: TaskResult,
    ) -> list[TraceEvent]:
        """Synthesize a P0 event stream from the collected TaskResult actions."""
        ts = _utcnow_iso()
        events: list[TraceEvent] = []
        for i, action in enumerate(result.actions):
            events.append(
                TraceEvent(
                    run_id=run_id,
                    task_id=task.task_id,
                    lab_id=lab.lab_id,
                    sut_id=image_id_of(task),
                    seq=0,  # collector reassigns seq
                    ts=ts,
                    event_type=EventType.TOOL_CALL,
                    data={"args": {"command": action.action}},
                    step_seq=i,
                )
            )
        events.append(
            TraceEvent(
                run_id=run_id,
                task_id=task.task_id,
                lab_id=lab.lab_id,
                sut_id=image_id_of(task),
                seq=0,
                ts=ts,
                event_type=EventType.TASK_END,
                data={"task_complete": result.task_complete},
            )
        )
        return events


def image_id_of(task: TaskRecord) -> str:
    """P0 placeholder SUT id for synthesized trace events."""
    return "sut-1"
