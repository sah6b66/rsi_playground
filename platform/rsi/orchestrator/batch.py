"""BatchRunner — batch state machine and 9-step task pipeline (DD-12 §4.1/§5).

P0 composition root: SandboxService (lab lifecycle + prepare/restore),
HostService (SUT instances), TraceCollector (event ingest/finalize), a
JudgeService-compatible judge, CheckpointStore, BudgetController, EventBus
and StatsAggregator. Pure orchestration: no device/container IO of its own.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import threading
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from rsi.core.enums import RunStatus
from rsi.core.errors import BudgetExceeded, EnvError
from rsi.core.models import (
    ApiEvalsetTask,
    BatchStats,
    DeviceSpec,
    Efficiency,
    EnvSpec,
    EvalTask,
    EventType,
    ExpectedAnswer,
    LabHandle,
    PathQuality,
    Scores,
    SshAccess,
    SafetyVerdict,
    TaskRecord,
    TaskResult,
    TaskSpec,
    Topology,
    TraceEvent,
    Verdict,
)
from rsi.core.naming import new_id
from rsi.log import get_logger

__all__ = ["BatchRunner", "api_task_to_task_record"]

logger = get_logger(__name__)

_PREPARE_ATTEMPTS = 3  # 1 initial + 2 retries (DD-12 §5.2 step 3)
_MAX_CONSECUTIVE_ENV_FAILURES = 3  # F1 circuit breaker (DD-12 §5.1)
_EST_TOKENS_PER_TASK = 50_000

_CRED_SUFFIXES = ("USER", "PASS", "HOST", "PORT")


def _cred_payload(resolved: Any) -> dict[str, Any] | None:
    """Normalize a resolver result into the build_dev_env cred payload.

    Accepts either an already-shaped dict payload (test stubs) or a
    ``ResolvedCreds`` (device_env maps device -> DEV_<X>_* vars); the latter
    is inverted into ``{"devices": {name: {user/pass/host/port}}}`` so
    ``build_dev_env`` derives the DEV_* environment.
    """
    if isinstance(resolved, dict):
        return resolved
    device_env = getattr(resolved, "device_env", None)
    if not isinstance(device_env, dict):
        return None
    devices: dict[str, dict[str, str]] = {}
    for name, vars_ in device_env.items():
        if not isinstance(vars_, dict):
            continue
        dev: dict[str, str] = {}
        for key, value in vars_.items():
            suffix = key.rsplit("_", 1)[-1]
            if suffix in _CRED_SUFFIXES and key != "DEV_RW":
                dev[suffix.lower()] = str(value)
        if dev:
            devices[str(name)] = dev
    payload: dict[str, Any] = {"devices": devices}
    api_key = getattr(resolved, "nms_token", None)
    if api_key:
        payload["api_key"] = api_key
    return payload


def persist_stdout(run_id: str, stdout: str | None, artifacts_root) -> "Path | None":
    """F1/C1: SUT 回答全文落 artifacts/<run_id>/stdout.txt（判分复核依据）.

    空输出/写失败均 no-op——留证不得阻断主流程。返回文件路径或 None。
    """
    if not (stdout or "").strip():
        return None
    try:
        out_dir = Path(artifacts_root) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "stdout.txt"
        path.write_text(stdout, encoding="utf-8")
        return path
    except OSError as exc:
        logger.warning("run.stdout_persist_failed", run_id=run_id, error=str(exc))
        return None


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sha16(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cache_key(task: TaskRecord, env: Any, config_version_id: str) -> tuple[str, str]:
    """ADR-5 content-hash cache key: (task_content_hash, env_content_hash).

    Both hashes are sha256[:16] over the canonical (sort_keys) JSON dump of
    the full model: the task hash covers prompt/prepare/expected/metrics —
    every field that can change the verdict — and the env hash covers
    devices/topology/ssh_access/config_apis (ssh_access kept in: conservative
    site-override policy per the ADR-5 erratum). ``config_version_id`` (the
    sut_config_git_sha slot in ADR-5) is compared at hit time instead of
    being part of the tuple, so a config change cannot silently reuse a
    verdict recorded under a different version.
    """
    task_json = json.dumps(
        task.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
        default=str,
    )
    env_json = json.dumps(
        env.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
        default=str,
    ) if hasattr(env, "model_dump") else json.dumps(
        repr(env), sort_keys=True, ensure_ascii=False
    )
    return _sha16(task_json), _sha16(env_json)


def _config_content_hash(config_version_id: str) -> str:
    """D-6 (ADR-5 约束注记的实现层加固): config 目录内容聚合哈希。

    When ``config_version_id`` is a P0 pass-through directory (path-as-id),
    an in-place edit of the directory must invalidate the cache entry. The
    hash aggregates every file's relative path + bytes; a nonexistent path
    (real GitStore version ids / offline ids) returns ``""`` — content
    validation is skipped and the hit check degrades to the plain version
    string comparison (GitStore exports are immutable, ADR-5 note).
    """
    if not config_version_id:
        return ""
    p = Path(config_version_id)
    if not p.is_dir():
        return ""
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(p)).replace("\\", "/").encode("utf-8"))
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:16]


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


def api_task_to_task_record(at: ApiEvalsetTask) -> TaskRecord:
    """Adapt an online API evalset task to the TaskRecord consumed by run_batch.

    ADR-27 D4: ``prepare`` is empty — environment preparation is handled by
    NetworkPrepClient via the carried ``network_env`` reference, not by CLI
    prepare actions. ``task``/``expected`` pass through unchanged.
    """
    return TaskRecord(
        task_id=at.task_id,
        title=at.title,
        prepare=[],
        task=at.task,
        expected=at.expected,
        network_env=at.network_env,
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
        result_cache: dict[tuple[str, str], tuple[str, str, Verdict, str]]
        | None = None,
        credential_resolver=None,
        instrumentor=None,
        session_factory=None,
        carrier_type: str = "opencode",
        batch_resolver: Callable[[dict], tuple[Any, list[TaskRecord], str, str]]
        | None = None,
        netprep=None,
    ) -> None:
        # credential_resolver (audit T2-1/B-8): object exposing
        # ``resolve_env(env, permission)`` (CredentialResolver) whose result
        # is converted to the build_dev_env cred payload; None keeps the
        # legacy cred=None behavior (backward compatible).
        self.credential_resolver = credential_resolver
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
        # H6/ADR-5: per-(task_content_hash, env_content_hash) verdict cache
        # shared across runner instances; a hit skips instance creation + SUT
        # execution. Value = (config_version_id, config_content_hash, verdict,
        # run_id) so a config change invalidates without key churn; D-6 added
        # the config content hash so an in-place edit of a pass-through config
        # directory also invalidates ("" = non-path id, string compare only).
        self.result_cache: dict[tuple[str, str], tuple[str, str, Verdict, str]] = (
            result_cache if result_cache is not None else {}
        )
        # D-4: default cache bypass (per-call override is preferred; this
        # attribute remains the runner-level default for noise calibration)
        self.bypass_cache: bool = False
        # API trigger wiring: optional resolver turning an API batch dict
        # (evalset_id/sut_id/params) into run_batch inputs
        # (env, tasks, config_version_id, image_ref). None -> offline default
        # (minimal fake env + one trivial task) so trigger() always works.
        self.batch_resolver = batch_resolver
        # ADR-27 D4: NetworkPrepClient for prep_mode="api" batches; None keeps
        # the runner CLI-only (an api batch then fails loudly at run_batch).
        self.netprep = netprep
        self._prep_mode: Literal["cli", "api"] = "cli"
        self._tasks: set[asyncio.Task] = set()
        self._envs: dict[str, Any] = {}
        # B-1: Instrumentor facade (DD-06 §6) — when wired, the exported config
        # version is instrumented (scan -> plan -> inject -> smoke verify)
        # before create_instance and the returned overlay dir becomes the
        # instance config input. None keeps the legacy copy-only overlay.
        self.instrumentor = instrumentor
        # B-2: optional DB session factory for instrumentation_reports rows
        self.session_factory = session_factory
        self.carrier_type = carrier_type
        self._instrument_cache: dict[tuple[str, str], tuple[Any, Any]] = {}
        # introspection for tests/ops: latest batch status and task->run_id map
        self.last_status: str = "created"
        self.last_run_ids: dict[str, str] = {}

    # -- API trigger (DD-13 §9.6 batch_runner wiring) ------------------------

    def trigger(
        self,
        batch: dict,
        *,
        bypass_cache: bool | None = None,
        on_update: Callable[[dict], None] | None = None,
    ) -> "BatchRunner":
        """Schedule ``run_batch`` for an API-created batch dict; non-blocking.

        Called from the async POST /batches and :rerun endpoints. The batch
        runs as a background asyncio task (kept alive via ``self._tasks`` so
        it is not garbage-collected mid-run); from a synchronous context the
        coroutine is pushed onto a daemon thread instead. Failures are
        contained: they set ``last_status`` and log a warning, never raising
        back into the endpoint.

        ``bypass_cache`` is per-call (D-4 audit fix): a rerun batch bypasses
        the cache without flipping the sticky runner-level default, so
        ordinary batches keep their cache hits.

        ``on_update`` (audit3 W2) is an optional completion callback invoked
        once per batch with a dict update (``status``/``progress``/
        ``finished_at``) — the API router uses it to sync the in-memory
        batch store with the background run. Callback errors are contained.

        Per-batch isolation (audit3 W3): each trigger runs on a shallow copy
        of this runner with a fresh ``last_run_ids``/``last_status`` (same
        semantics as ``ParallelBatchRunner.run_many``), so concurrent
        triggers cannot cross-write each other's introspection state. The
        shared result cache / checkpoint store / services stay shared (they
        are keyed externally). Returns the per-batch copy for introspection.
        """
        batch_id = str(batch.get("id") or new_id("batch"))
        runner = copy.copy(self)
        runner.last_run_ids = {}
        runner.last_status = "created"
        coro = runner._run_triggered_batch(batch, batch_id, bypass_cache, on_update)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # sync caller: run on a daemon thread
            threading.Thread(
                target=asyncio.run,
                args=(coro,),
                daemon=True,
                name=f"rsi-batch-{batch_id}",
            ).start()
            return runner
        task = loop.create_task(coro, name=f"rsi-batch-{batch_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return runner

    async def _run_triggered_batch(
        self,
        batch: dict,
        batch_id: str,
        bypass_cache: bool | None,
        on_update: Callable[[dict], None] | None = None,
    ) -> None:
        params = dict(batch.get("params") or {})
        if bypass_cache is None:
            bypass_cache = bool(params.get("bypass_cache", False))

        def _notify(update: dict) -> None:
            if on_update is None:
                return
            try:
                on_update(update)
            except Exception as exc:  # noqa: BLE001 - callback must not kill the run
                logger.warning(
                    "batch.trigger_on_update_failed",
                    batch_id=batch_id,
                    error=str(exc),
                )

        # W-B: forward per-task progress to on_update while running (the
        # completion notification below keeps its original semantics).
        def _progress(done: int, total: int) -> None:
            _notify({
                "status": "running",
                "progress": {"done": done, "total": total},
            })

        try:
            env, tasks, config_version_id, image_ref = (
                await self._resolve_trigger_inputs(batch)
            )
            stats = await self.run_batch(
                batch_id,
                env,
                tasks,
                config_version_id,
                image_ref,
                # API 批可按 params 显式提权（S11 真实故障修复任务需要
                # read-write：DEV_RW 注入 + judge 安全语义随权限走）
                permission=str(params.get("permission") or "read-only"),
                bypass_cache=bypass_cache,
                prep_mode=str(batch.get("prep_mode") or "cli"),
                db_meta={
                    "evalset_id": str(batch.get("evalset_id", "")),
                    "sut_id": str(batch.get("sut_id", "")),
                    "profile_id": batch.get("profile_id"),
                },
                progress_fn=_progress,
            )
        except asyncio.CancelledError:
            # W-A: shutdown cancelled the in-flight batch — record a terminal
            # aborted state (memory + DB + store) before the cancellation
            # unwinds, then re-raise so the task ends cancelled.
            self.last_status = "aborted"
            logger.warning("batch.trigger_cancelled", batch_id=batch_id)
            # W-D: sync DB write off the event loop
            await asyncio.to_thread(
                self._db_finish_batch, batch_id, "aborted", None
            )
            _notify({
                "status": "aborted",
                "progress": {"done": 0, "total": 0},
                "finished_at": _utcnow_iso(),
            })
            raise
        except Exception as exc:  # noqa: BLE001 - trigger must not 500 the API
            self.last_status = "failed"
            logger.warning(
                "batch.trigger_failed", batch_id=batch_id, error=str(exc)
            )
            _notify({
                "status": "failed",
                "progress": {"done": 0, "total": 0},
                "finished_at": _utcnow_iso(),
            })
            return
        n = getattr(stats, "n", 0)
        _notify({
            "status": self.last_status,
            "progress": {"done": n, "total": max(len(tasks), n)},
            "finished_at": _utcnow_iso(),
        })

    async def _resolve_trigger_inputs(
        self, batch: dict
    ) -> tuple[Any, list[TaskRecord], str, str]:
        """Batch dict -> (env, tasks, config_version_id, image_ref).

        Uses the injected resolver when present; otherwise the offline
        default (minimal fake env + one trivial task) keeps the API path
        functional in the offline assembly. The resolver may be async (D4:
        the api-prep path fetches tasks from the online Evalset API); the
        returned awaitable is awaited here.
        """
        if self.batch_resolver is not None:
            result = self.batch_resolver(batch)
            if inspect.isawaitable(result):
                result = await result
            return result
        return default_trigger_inputs(batch)

    def resolve_inputs(self, batch: dict) -> tuple[Any, list[TaskRecord], str, str]:
        """Sync public wrapper (sync resolvers only).

        Production callers (noise-calibration bridge) resolve an API batch
        dict without going through the trigger background-task machinery.
        An async resolver (api-prep fetch) returns a coroutine here — the
        caller is expected to await it.
        """
        if self.batch_resolver is not None:
            return self.batch_resolver(batch)
        return default_trigger_inputs(batch)

    # -- batch loop (§5.1) ---------------------------------------------------

    async def run_batch(
        self,
        batch_id: str,
        env,
        tasks: list[TaskRecord],
        config_version_id: str,
        image_ref: str,
        permission: str = "read-only",
        bypass_cache: bool | None = None,
        db_meta: dict[str, str] | None = None,
        prep_mode: Literal["cli", "api"] = "cli",
        progress_fn: Callable[[int, int], None] | None = None,
    ) -> BatchStats:
        # ADR-27 D4: prep_mode routes environment lifecycle. "cli" (default)
        # keeps the SandboxService lab pipeline untouched; "api" skips the
        # lab entirely (no build/wait_ready/snapshot/destroy) and prepares/
        # restores the network environment per task via NetworkPrepClient.
        self._prep_mode = prep_mode
        if prep_mode == "api":
            if self.netprep is None:
                raise EnvError(
                    "NetworkPrepClient not configured (prep_mode='api' "
                    "requires settings.network_prep_api.url)",
                    context={"batch_id": batch_id},
                )
            # env is intentionally ignored on this path; tasks carry their
            # own network_env references.
            env = None
        # W-B: optional per-task progress callback (done, total) so callers
        # (API trigger) can surface running-state progress; None keeps the
        # legacy completion-only notification.
        # D-4: None -> runner default; noise-calibration batches force bypass
        # (ADR-17) so repeated runs always execute.
        if bypass_cache is None:
            bypass_cache = self.bypass_cache
        if self._batch_purpose(batch_id) == "noise_calibration":
            bypass_cache = True
        # D-6: config 目录内容哈希（path-as-id 场景）；非路径 id 返回 ""
        # （仅字符串比对，GitStore 导出不可变 — ADR-5 约束注记）
        cfg_content_hash = _config_content_hash(config_version_id)
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
        # API mode (D4): no lab — placeholder handles only carry identifiers
        # (run rows / trace events / checkpoints); every SandboxService call
        # is skipped.
        if self._prep_mode == "api":
            lab = LabHandle(lab_id="", host=self.host, backend="api", endpoints={})
            snap0 = SimpleNamespace(snapshot_id="")
        else:
            lab, snap0 = await self._build_or_reuse_lab(env, state)
        self._envs[batch_id] = env

        verdicts: list[Verdict] = []
        verdict_task_ids: list[str] = []
        status = "finalized"
        consecutive_env_failures = 0
        self.last_status = "running"
        await self.event_bus.publish("batch", batch_id, "created", "running")
        # F10: batches/runs persistence (IN-016 run->batch->config_version
        # DB reverse lookup) — best effort, never blocks the batch.
        # W-D: sync DB writes run off the event loop (to_thread).
        await asyncio.to_thread(
            self._db_insert_batch,
            batch_id, config_version_id, permission, db_meta,
        )

        def _report_progress() -> None:
            if progress_fn is None:
                return
            try:
                progress_fn(len(completed), len(tasks))
            except Exception as exc:  # noqa: BLE001 - progress must not kill the run
                logger.warning(
                    "run_batch.progress_failed", batch_id=batch_id, error=str(exc)
                )

        # B-21: ANY exception leaving the task loop must still walk the full
        # terminal path (last_status + event_bus + _db_finish_batch) before
        # propagating, otherwise the batches row stays "running" forever.
        task_exc: BaseException | None = None
        cancelled = False
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

                # c. cache hit (H6/ADR-5): identical task+env content hashes
                # recorded under the same config version -> reuse the cached
                # verdict, skip execution entirely. D-6: when the version id
                # is a live directory (P0 pass-through config), recompute its
                # content hash — an in-place edit invalidates the entry.
                cache_key = _cache_key(task, env, config_version_id)
                hit = None if bypass_cache else self.result_cache.get(cache_key)
                if hit is not None and hit[0] != config_version_id:
                    hit = None  # same content, different config version
                if hit is not None and hit[1] and hit[1] != cfg_content_hash:
                    hit = None  # D-6: config dir mutated in place -> miss
                if hit is not None:
                    _, _, verdict, run_id = hit
                    logger.debug("run_batch.cache_hit", task_id=task.task_id, run_id=run_id)
                    verdicts.append(verdict)
                    verdict_task_ids.append(task.task_id)
                    self.last_run_ids[task.task_id] = run_id
                    completed.append(
                        {
                            "task_id": task.task_id,
                            "run_id": run_id,
                            "status": "cached",
                        }
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
                    await self.event_bus.publish("run", run_id, "judging", "cached")
                    # F10: cached hits reuse the original run_id — insert only
                    # when no row exists yet (fresh DB / cleared cache entry).
                    await asyncio.to_thread(
                        self._db_insert_run,
                        batch_id, task.task_id, run_id, "cached", seq,
                        lab.lab_id, cache_key, verdict,
                    )
                    _report_progress()
                    continue

                # d. run task (9-step pipeline)
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
                verdict_task_ids.append(task.task_id)
                run_status = (
                    RunStatus.ENV_FAILED if verdict.missing else RunStatus.DONE
                )
                # F10: persist the executed run row
                await asyncio.to_thread(
                    self._db_insert_run,
                    batch_id,
                    task.task_id,
                    self.last_run_ids.get(task.task_id, ""),
                    run_status.value,
                    seq,
                    lab.lab_id,
                    cache_key,
                    verdict,
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
                if not verdict.missing and not bypass_cache:
                    self.result_cache[cache_key] = (
                        config_version_id,
                        cfg_content_hash,
                        verdict,
                        self.last_run_ids.get(task.task_id, ""),
                    )
                # D-5: global budget circuit breaker — tier-2 check after each
                # task; a breach writes the STOP file + alert (inside
                # check_global) and aborts the batch like the stop file does.
                try:
                    await self.budget.check_global()
                except BudgetExceeded:
                    status = "aborted"
                    logger.debug("run_batch.abort", reason="global_budget", seq=seq)
                    await self._publish_alert(
                        "critical", "全局预算超限，批次中止", scope=batch_id
                    )
                    break
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
                _report_progress()
        except asyncio.CancelledError:
            # W-A: shutdown cancellation is a terminal state, not a leak —
            # mark the batches row aborted before the cancellation unwinds.
            status = "aborted"
            cancelled = True
            logger.warning(
                "run_batch.cancelled", batch_id=batch_id
            )
        except Exception as exc:  # noqa: BLE001 - B-21 terminal-state guarantee
            status = "failed"
            task_exc = exc
            logger.warning(
                "run_batch.task_exception",
                batch_id=batch_id,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
        finally:
            # 5. destroy lab (always, also on abort) — API mode has no lab
            if self._prep_mode != "api":
                await self.sandbox.destroy(lab)
            self._envs.pop(batch_id, None)

        self.last_status = status
        await self.event_bus.publish("batch", batch_id, "running", status)
        # 4. aggregate verdicts -> BatchStats
        stats = self.stats_agg.aggregate(verdicts, task_ids=verdict_task_ids)
        await asyncio.to_thread(self._db_finish_batch, batch_id, status, stats)
        logger.debug(
            "run_batch.exit", batch_id=batch_id, status=status, n=stats.n
        )
        if task_exc is not None:
            raise task_exc
        if cancelled:
            # propagate the cancellation after the terminal path has run
            raise asyncio.CancelledError
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

        # 1. prepare (backup + exec_cli/call_api), retry <= 2, then env_failed.
        #    DD-12 §5.2 order: restore_task -> prepare between attempts, so a
        #    partially applied prepare never leaks into the retry.
        #    API mode (D4): prepare goes through NetworkPrepClient using the
        #    task's network_env reference; no sandbox lab involved.
        applied: Any = []
        prepared = False
        if self._prep_mode == "api":
            prep_result = await self._netprep_prepare(batch_id, task)
            if prep_result is None:
                logger.debug("run_task.exit", run_id=run_id, status="env_failed")
                return TaskResult(task_complete=False), _env_failed_verdict()
            applied = prep_result
            prepared = True
        if not prepared:
            for _attempt in range(self.prepare_attempts):
                try:
                    applied = await self.sandbox.prepare(lab, task.task_id, task.prepare)
                    prepared = True
                    break
                except EnvError as exc:
                    logger.debug(
                        "run_task.prepare_retry", task_id=task.task_id, exc_type=type(exc).__name__
                    )
                    # best-effort cleanup of the partial prepare before retrying
                    try:
                        await self.sandbox.restore_task(lab, applied)
                    except Exception as cleanup_exc:  # noqa: BLE001 - retry anyway
                        logger.debug(
                            "run_task.prepare_restore_failed",
                            task_id=task.task_id,
                            exc_type=type(cleanup_exc).__name__,
                        )
                    continue
        if not prepared:
            logger.debug("run_task.exit", run_id=run_id, status="env_failed")
            result = TaskResult(task_complete=False)
            return result, _env_failed_verdict()

        # 4. create SUT instance (P0: one instance per run).
        #    API mode (D4): no device credentials — DEV_* is never injected;
        #    the SUT talks to the network env through the prep API.
        cred = (
            None if self._prep_mode == "api"
            else self._resolve_cred(batch_id, permission)
        )
        effective_config = config_version_id
        # An empty/placeholder config_version_id must never resolve to cwd
        # (``Path("")`` is ``.``) — overlaying cwd would copy the whole tree.
        if (
            self.instrumentor is not None
            and bool(str(config_version_id).strip())
            and Path(config_version_id).is_dir()
        ):
            # B-1 (DD-06 §6): instrument the exported config version once per
            # (batch, config_version) and feed the returned overlay dir to
            # create_instance. Smoke runs on throwaway instances.
            cache_key = (batch_id, config_version_id)
            if cache_key not in self._instrument_cache:
                # B-22: an instrumentor crash (Scanner/Injector/copytree —
                # disk full, permissions, nested-dir ValueError) must degrade
                # to the original config dir (same semantics as the facade's
                # verify-failure fallback, B-1), never kill the batch.
                try:
                    # B-23: the instrument pipeline (directory scan + overlay
                    # copytree + injection + two smoke collects of up to 60s
                    # each) is pure synchronous IO — run it on a worker
                    # thread so the event loop keeps scheduling. The smoke
                    # callback (_smoke_run) only touches sync carrier methods
                    # and is thread-safe (no loop-bound state).
                    overlay_dir, report = await asyncio.to_thread(
                        self.instrumentor.instrument,
                        Path(config_version_id),
                        self.carrier_type,
                        lambda cfg: self._smoke_run(lab, image_ref, cfg),
                    )
                except Exception as exc:  # noqa: BLE001 - degrade, don't crash
                    logger.warning(
                        "run_task.instrument_failed",
                        run_id=run_id,
                        exc_type=type(exc).__name__,
                        error=str(exc),
                    )
                else:
                    self._instrument_cache[cache_key] = (overlay_dir, report)
                    self._record_instrumentation(
                        batch_id, config_version_id, report
                    )
            if cache_key in self._instrument_cache:
                effective_config = str(self._instrument_cache[cache_key][0])
        # B-22: create_instance failures (CarrierError — host unconfigured,
        # net attach failed) are TASK-level env faults: restore the prepare
        # state, score the task env_failed and keep the batch alive (DD-12
        # task-level env_failed semantics, feeds the F1 circuit breaker).
        try:
            instance = self.carrier.create_instance(
                image_ref=image_ref,
                config_version_id=effective_config,
                lab=lab,
                permission=permission,
                instrument=True,
                host=self.host,
                cred=cred,
                run_id=run_id,
                carrier_type=self.carrier_type,
            )
        except Exception as exc:
            logger.warning(
                "run_task.create_instance_failed",
                run_id=run_id,
                task_id=task.task_id,
                exc_type=type(exc).__name__,
                error=str(exc),
            )
            try:
                if self._prep_mode == "api":
                    await self._netprep_restore(task, applied)
                else:
                    await self.sandbox.restore_task(lab, applied)
            except Exception as cleanup_exc:  # noqa: BLE001 - degrading anyway
                logger.debug(
                    "run_task.create_instance_restore_failed",
                    task_id=task.task_id,
                    exc_type=type(cleanup_exc).__name__,
                )
            logger.debug("run_task.exit", run_id=run_id, status="env_failed")
            return TaskResult(task_complete=False), _env_failed_verdict()
        # F-2 (audit3): destroy the instance on EVERY exit path (normal,
        # instance_crashed early return, judge/restore exceptions). A leaked
        # handle keeps the HostService._instances entry and the host_pool
        # slot count alive, permanently polluting least-loaded scheduling.
        destroyed = False

        def _destroy_instance() -> None:
            nonlocal destroyed
            if destroyed:
                return
            destroyed = True
            try:
                self.carrier.destroy(instance)
            except Exception:  # noqa: BLE001 - teardown must not mask results
                logger.debug(
                    "run_task.destroy_failed",
                    run_id=run_id,
                    instance_id=getattr(instance, "instance_id", "?"),
                )

        try:
            try:
                # 5. submit -> collect (timeout from config)
                handle = self.carrier.submit_task(instance, task.task)
                result = await asyncio.to_thread(
                    self.carrier.collect, handle, self.task_timeout_s
                )
                # F1/C1: 回答全文留证（判分复核/排障），失败不阻断
                if self.settings is not None:
                    persist_stdout(
                        run_id, getattr(handle, "stdout", None),
                        self.settings.artifacts_dir,
                    )
                # F2 crash classification: a carrier that surfaces the container
                # exit code (137=OOM / 139=segfault) marks the run infra-crashed
                # regardless of stdout; FINAL_JSON is meaningless then. Carriers
                # without an exit_code field are skipped (F6 adds it).
                exit_code = getattr(handle, "exit_code", None)
                if exit_code in (137, 139) and not result.instance_crashed:
                    logger.debug(
                        "run_task.instance_crashed", run_id=run_id, exit_code=exit_code
                    )
                    result = TaskResult(task_complete=False, instance_crashed=True)

                if result.instance_crashed:
                    # infra fault, not an Agent defect: skip judgment entirely
                    # (excluded from attribution samples, counted in infra stats)
                    logger.debug(
                        "run_task.exit", run_id=run_id, status="instance_crashed"
                    )
                    return result, _env_failed_verdict()

                # 6. collect trace events (P0: synthesized from the result
                #    actions + wiretap channel files tailed into the collector)
                events = self._trace_events(run_id, task, lab, result)
                # F1.2 (audit4): tail the carrier's wiretap ndjson channel(s)
                # so real OpenCode runs contribute LLM_REQUEST/usage events
                # (one-shot read; missing files are skipped quietly).
                events.extend(
                    self._ingest_trace_channels(run_id, task, lab, instance)
                )
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
                # T2-5: judge context so the LLM can tell baseline vs candidate
                config_version_id=config_version_id,
                batch_purpose=self._batch_purpose(batch_id),
            )

            # 8. task-level restore AFTER judgment (final-state asserts need the
            # fault state present, ADR-20 L1). restore_task already retries
            # internally; on persistent failure escalate to L2 environment-level
            # recovery (lab reset + restore replay), and if even that fails the
            # TASK is scored env_failed (batch-level failure semantics, DD-12
            # §5.2) instead of killing the whole batch.
            restored = False
            if self._prep_mode == "api":
                # D4: API mode restore = netprep.restore(env_name, env_id,
                # params) with the same retry policy; no L2 lab recovery
                # exists (no lab) — persistent failure scores env_failed.
                for _attempt in range(3):
                    try:
                        await self._netprep_restore(task, applied)
                        restored = True
                        break
                    except EnvError as exc:
                        logger.debug(
                            "run_task.netprep_restore_retry",
                            run_id=run_id,
                            attempt=_attempt + 1,
                            exc_type=type(exc).__name__,
                        )
                if not restored:
                    return result, _env_failed_verdict()
            else:
                for _attempt in range(3):  # 1 initial + 2 retries (D-13)
                    try:
                        await self.sandbox.restore_task(lab, applied)
                        restored = True
                        break
                    except EnvError as exc:
                        logger.debug(
                            "run_task.restore_retry",
                            run_id=run_id,
                            attempt=_attempt + 1,
                            exc_type=type(exc).__name__,
                        )
            if not restored:
                try:
                    # L2 (B-24, DD-12 §5.2): environment-level recovery back
                    # to the batch-initial state — lab reset, then restore
                    # the snap0 snapshot (the pre-task baseline the batch
                    # started from; a bare reset only reaches the built-clean
                    # state, not the post-build snap0 state), then replay the
                    # lab's ops.jsonl journalled prepare ops so state applied
                    # since snap0 is rebuilt (exec_cli records are re-sent;
                    # call_api records are skipped with a warning — they are
                    # config-API writes the snapshot may already carry and
                    # are not idempotent). The failing task's own prepare is
                    # excluded: this restore exists precisely to undo it.
                    logger.warning("run_task.restore_l2_recovery", run_id=run_id)
                    await self.sandbox.reset(lab)
                    snapshots = getattr(self.sandbox, "snapshots", None)
                    snap_ref = None
                    if callable(snapshots):
                        snap_ref = next(
                            (
                                s
                                for s in snapshots(lab.lab_id)
                                if s.snapshot_id == snap0_id
                            ),
                            None,
                        )
                    if snap_ref is not None:
                        await self.sandbox.restore(lab, snap_ref)
                    else:
                        logger.warning(
                            "run_task.restore_l2_snapshot_missing",
                            run_id=run_id,
                            snapshot_id=snap0_id,
                        )
                    replay = getattr(self.sandbox, "replay_applied_ops", None)
                    if callable(replay):
                        replayed = await replay(
                            lab, exclude_task_ids={task.task_id}
                        )
                        logger.debug(
                            "run_task.restore_l2_replay",
                            run_id=run_id,
                            replayed=replayed,
                        )
                    restored = True
                except Exception as exc:  # noqa: BLE001 - task degrades, not batch
                    logger.warning(
                        "run_task.restore_env_failed",
                        run_id=run_id,
                        exc_type=type(exc).__name__,
                    )
            if not restored:
                return result, _env_failed_verdict()

            logger.debug(
                "run_task.exit",
                run_id=run_id,
                task_complete=result.task_complete,
                missing=verdict.missing,
            )
            return result, verdict
        finally:
            _destroy_instance()

    # -- helpers ----------------------------------------------------------------

    def _smoke_run(self, lab: LabHandle, image_ref: str, config_dir: Path):
        """B-1 smoke callback for Instrumentor.instrument (DD-06 §4 double run).

        Runs a trivial probe task on a throwaway non-instrumented instance
        built from ``config_dir``; carriers without a canned response yield a
        parse-error TaskResult, which is still a comparable signal (base vs
        overlay equivalence)."""
        instance = None
        try:
            instance = self.carrier.create_instance(
                image_ref=image_ref,
                config_version_id=str(config_dir),
                lab=lab,
                permission="read-only",
                instrument=False,
                host=self.host,
                cred=None,
                run_id="instrument-smoke",
                carrier_type=self.carrier_type,
            )
            handle = self.carrier.submit_task(
                instance, TaskSpec(prompt="instrumentor smoke probe")
            )
            return self.carrier.collect(handle, 60.0)
        except Exception:  # noqa: BLE001 - smoke must degrade, not crash
            return TaskResult(task_complete=False)
        finally:
            if instance is not None:
                try:
                    self.carrier.destroy(instance)
                except Exception:  # noqa: BLE001
                    pass

    # -- F10: batches/runs persistence ------------------------------------

    # ck_batches_purpose has no 'adhoc_eval' member; a plain (non-iteration)
    # batch is closest to a baseline eval of the given config version.
    _DB_PURPOSE_MAP = {"adhoc_eval": "baseline_eval"}

    def _db_insert_batch(
        self,
        batch_id: str,
        config_version_id: str,
        permission: str,
        db_meta: dict[str, str] | None,
    ) -> None:
        """INSERT the batches row at batch start. Best effort — never raises.

        ``db_meta`` carries the caller-side identifiers BatchRunner itself
        does not know: ``evalset_id``/``sut_id`` (required NOT NULL columns;
        SQLite FK enforcement is off in this deployment, so empty-string
        placeholders are used when the caller cannot supply them),
        ``iteration_id`` and ``profile_id`` (nullable).
        """
        if self.session_factory is None:
            return
        try:
            from rsi.db.tables import Batch

            meta = db_meta or {}
            purpose = self._batch_purpose(batch_id)
            with self.session_factory() as session:
                session.add(
                    Batch(
                        id=batch_id,
                        evalset_id=str(meta.get("evalset_id", "")),
                        sut_id=str(meta.get("sut_id", "")),
                        config_version_id=config_version_id,
                        purpose=self._DB_PURPOSE_MAP.get(purpose, purpose),
                        optimization_profile_id=meta.get("profile_id"),
                        status="running",
                        iteration_id=meta.get("iteration_id"),
                        permission=permission,
                        created_at=_utcnow_iso(),
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - DB write must not fail runs
            logger.warning(
                "batch.persist_failed", batch_id=batch_id, error=str(exc)
            )

    def _db_finish_batch(self, batch_id: str, status: str, stats: Any) -> None:
        """UPDATE the batches row at batch end (status/finished_at/stats)."""
        if self.session_factory is None:
            return
        try:
            from rsi.db.tables import Batch

            stats_json = None
            if hasattr(stats, "model_dump_json"):
                stats_json = stats.model_dump_json()
            with self.session_factory() as session:
                row = session.get(Batch, batch_id)
                if row is None:
                    return
                row.status = status
                row.finished_at = _utcnow_iso()
                row.stats_json = stats_json
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "batch.persist_finish_failed", batch_id=batch_id, error=str(exc)
            )

    def _db_insert_run(
        self,
        batch_id: str,
        task_id: str,
        run_id: str,
        status: str,
        seq: int,
        lab_id: str | None,
        cache_key: tuple[str, str] | None,
        verdict: Any,
    ) -> None:
        """INSERT one runs row after run_task completes (or on a cache hit).

        Cache hits reuse the original run_id; if that row already exists the
        insert is skipped (PK conflict would abort the batch otherwise).
        """
        if self.session_factory is None or not run_id:
            return
        try:
            from rsi.db.tables import Run

            summary = None
            if verdict is not None and hasattr(verdict, "scores"):
                try:
                    summary = json.dumps(
                        {
                            "task_complete": getattr(
                                verdict.scores, "task_complete", None
                            ),
                            "scores": verdict.scores.model_dump(mode="json"),
                            "missing": bool(getattr(verdict, "missing", False)),
                        },
                        ensure_ascii=False,
                    )
                except Exception:  # noqa: BLE001 - summary is optional
                    summary = None
            now = _utcnow_iso()
            with self.session_factory() as session:
                if session.get(Run, run_id) is not None:
                    return
                session.add(
                    Run(
                        id=run_id,
                        batch_id=batch_id,
                        eval_task_id=task_id,
                        lab_id=lab_id,
                        exec_host=self.host,
                        status=status,
                        cache_key=(
                            f"{cache_key[0]}/{cache_key[1]}"
                            if isinstance(cache_key, tuple)
                            else None
                        ),
                        result_summary_json=summary,
                        started_at=now,
                        ended_at=now,
                        seq_in_batch=seq,
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "run.persist_failed", batch_id=batch_id, run_id=run_id,
                error=str(exc),
            )

    def _record_instrumentation(
        self, batch_id: str, config_version_id: str, report: Any
    ) -> None:
        """B-2: persist the CoverageReport to instrumentation_reports.

        Best effort — a DB failure only logs a warning."""
        if self.session_factory is None:
            return
        try:
            import uuid

            from rsi.db.tables import InstrumentationReport

            plan_summary = {
                "carrier": self.carrier_type,
                "config_version_id": config_version_id,
                "covered": report.model_dump(mode="json")["covered"],
            }
            with self.session_factory() as session:
                session.add(
                    InstrumentationReport(
                        id=str(uuid.uuid4()),
                        batch_id=batch_id,
                        plan_json=json.dumps(plan_summary, ensure_ascii=False),
                        coverage_json=report.model_dump_json(),
                        created_at=_utcnow_iso(),
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - report write must not fail runs
            logger.warning(
                "instrumentation_report.persist_failed",
                batch_id=batch_id,
                error=str(exc),
            )

    @staticmethod
    def _batch_purpose(batch_id: str) -> str:
        """Derive the batch purpose from the batch id prefix (T2-5 context).

        IterationEngine names its verification batches with a
        ``verify-<tier>-...`` prefix; everything else is a plain eval batch.
        """
        lower = batch_id.lower()
        if lower.startswith("verify-smoke"):
            return "smoke"
        if lower.startswith("verify-screen"):
            return "iterate_screen"
        if lower.startswith("verify-holdout"):
            return "candidate_eval"
        if "baseline" in lower:
            return "baseline_eval"
        if "noise" in lower:
            return "noise_calibration"
        return "adhoc_eval"

    async def _netprep_prepare(self, batch_id: str, task: TaskRecord):
        """D4 API-mode prepare via NetworkPrepClient; None -> env_failed.

        Retries follow the same prepare_attempts budget as the CLI path; a
        failed external prepare leaves nothing to roll back locally.
        """
        env_ref = task.network_env
        if env_ref is None:
            logger.warning(
                "run_task.netprep_no_env_ref", task_id=task.task_id,
            )
            return None
        for _attempt in range(self.prepare_attempts):
            try:
                result = await self.netprep.prepare(
                    env_ref.name, dict(env_ref.params), session_id=batch_id
                )
                return result
            except EnvError as exc:
                logger.debug(
                    "run_task.netprep_prepare_retry",
                    task_id=task.task_id,
                    attempt=_attempt + 1,
                    exc_type=type(exc).__name__,
                )
        return None

    async def _netprep_restore(self, task: TaskRecord, prep_result: Any) -> None:
        """D4 API-mode restore: netprep.restore(env_name, env_id, params)."""
        env_ref = task.network_env
        if env_ref is None or prep_result is None:
            return
        await self.netprep.restore(
            env_ref.name, str(getattr(prep_result, "env_id", "")), dict(env_ref.params)
        )

    def _resolve_cred(self, batch_id: str, permission: str) -> Any:
        """Resolve the per-run credential payload for create_instance (T2-1).

        Uses the injected resolver against the batch's EnvSpec; any failure
        degrades to ``None`` (legacy behavior) rather than failing the run.
        """
        if self.credential_resolver is None:
            return None
        env = self._envs.get(batch_id)
        if env is None:
            return None
        try:
            resolved = self.credential_resolver.resolve_env(env, permission)
        except Exception as exc:  # noqa: BLE001 - creds must not break the batch
            logger.warning(
                "cred.resolve_failed",
                batch_id=batch_id,
                exc_type=type(exc).__name__,
            )
            return None
        return _cred_payload(resolved)

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

    def _ingest_trace_channels(
        self,
        run_id: str,
        task: TaskRecord,
        lab: LabHandle,
        instance: Any,
    ) -> list[TraceEvent]:
        """Tail the carrier's ndjson trace channels into the collector (F1.2).

        P0 one-shot semantics: each host-visible channel file is read once
        after collect (before finalize). Channel paths that do not exist on
        the host (offline/fake carriers, unmapped container volumes) are
        skipped quietly. Returns the wiretap-derived events so callers can
        include them in the events handed to the judge.
        """
        try:
            channels = self.carrier.trace_channels(instance)
        except Exception as exc:  # noqa: BLE001 - tracing must not fail runs
            logger.debug(
                "trace_channels.unavailable",
                run_id=run_id,
                exc_type=type(exc).__name__,
            )
            return []
        if getattr(channels, "kind", None) != "ndjson":
            return []
        for raw_path in getattr(channels, "paths", None) or []:
            path = Path(raw_path)
            if not path.is_file():
                logger.debug("trace_channels.skip_missing", run_id=run_id, path=raw_path)
                continue
            try:
                self.collector.tail_ndjson(
                    path,
                    run_id,
                    {
                        "task_id": task.task_id,
                        "lab_id": lab.lab_id,
                        "sut_id": image_id_of(task),
                        "run_id": run_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - best effort trace ingest
                logger.warning(
                    "trace_channels.tail_failed",
                    run_id=run_id,
                    path=str(path),
                    exc_type=type(exc).__name__,
                )
        return self.collector.events(run_id)

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


def default_trigger_inputs(batch: dict) -> tuple[Any, list[TaskRecord], str, str]:
    """Offline canned (env, tasks, config_version_id, image_ref).

    Shared by ``BatchRunner._resolve_trigger_inputs`` (resolver=None
    fallback) and the production resolver's honest-degradation path
    (evalset record without an env.yaml/tasks payload) so both produce
    identical defaults.
    """
    params = dict(batch.get("params") or {})
    env = EnvSpec(
        name=f"lab-{str(batch.get('id') or 'adhoc')}",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw")
        },
    )
    tasks = [
        TaskRecord(
            task_id="fault/adhoc-00",
            title="API-triggered ad-hoc task",
            prepare=[],
            task=TaskSpec(prompt="diagnose the fault and reply with FINAL_JSON"),
            expected=ExpectedAnswer(),
        )
    ]
    config_version_id = str(params.get("config_version_id", ""))
    image_ref = str(batch.get("sut_id") or params.get("image_ref") or "sut")
    return env, tasks, config_version_id, image_ref


def image_id_of(task: TaskRecord) -> str:
    """P0 placeholder SUT id for synthesized trace events."""
    return "sut-1"
