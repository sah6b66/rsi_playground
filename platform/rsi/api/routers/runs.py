"""Run endpoints (DD-13 §9.7). P0: in-memory store."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from rsi.api.db_reads import get_run_record
from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.enums import RunStatus
from rsi.core.errors import (
    ConfigError,
    InvalidTransitionError,
    LLMOutputInvalid,
    NotFoundError,
    ValidationError,
)
from rsi.core.models import PairwiseReport
from rsi.security.auth import Claims

router = APIRouter(prefix="/runs", tags=["runs"])

_TERMINAL_RUN = {RunStatus.DONE, RunStatus.ENV_FAILED, RunStatus.SUT_TIMEOUT,
                 RunStatus.ABORTED, RunStatus.CACHED}


class RunOut(BaseModel):
    id: str
    batch_id: str
    task_id: str | None = None
    status: str
    result: dict[str, Any] | None = None


class TracePage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    has_more: bool = False


def _get(store: dict, run_id: str) -> dict:
    try:
        return store[run_id]
    except KeyError:
        raise NotFoundError(
            f"run {run_id!r} not found", context={"run_id": run_id}
        ) from None


def _get_anywhere(request: Request, store: dict, run_id: str) -> dict:
    """Memory-first run lookup with a DB fallback (audit4 W-C)."""
    run = get_run_record(request, run_id, store)
    if run is None:
        raise NotFoundError(
            f"run {run_id!r} not found", context={"run_id": run_id}
        )
    return run


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
) -> RunOut:
    return RunOut(**_get_anywhere(request, store, run_id))


@router.get("/{run_id}/stdout")
async def get_run_stdout(
    run_id: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
) -> Response:
    """F1/C1: run 的 SUT 回答全文（artifacts/<run_id>/stdout.txt）."""
    _get_anywhere(request, store, run_id)
    settings = request.app.state.services.get("settings")
    path = (
        settings.artifacts_dir / run_id / "stdout.txt"
        if settings is not None
        else None
    )
    if path is None or not path.is_file():
        raise NotFoundError(
            f"stdout artifact for run {run_id!r} not found",
            context={"run_id": run_id},
        )
    return Response(
        content=path.read_text(encoding="utf-8", errors="replace"),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/{run_id}/traces", response_model=TracePage)
async def list_traces(
    run_id: str,
    request: Request,
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
    traces: dict = Depends(get_store("traces")),
) -> TracePage:
    _get_anywhere(request, store, run_id)
    if traces.get(run_id):
        events = sorted(traces.get(run_id, []), key=lambda e: e["seq"])
    else:
        # F2/W12：生产事件在 collector——内存 traces store 空时回退
        from rsi.api.routers.traces import _collector_events

        events = sorted(_collector_events(request, run_id), key=lambda e: e["seq"])
    items, page = paginate(events, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return TracePage(
        items=items,
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


@router.post("/{run_id}:re-judge", response_model=RunOut, status_code=202)
@audit.audited("run.rejudge", object_type="run")
async def rejudge_run(
    run_id: str,
    request: Request,
    user: Claims = Depends(require_perms("iteration:manage")),
    store: dict = Depends(get_store("runs")),
) -> RunOut:
    run = _get_anywhere(request, store, run_id)
    if RunStatus(run["status"]) in _TERMINAL_RUN and run["status"] != RunStatus.DONE.value:
        raise InvalidTransitionError(
            f"cannot re-judge run in terminal state {run['status']}",
            context={"run_id": run_id, "from_state": run["status"]},
        )
    run["status"] = RunStatus.JUDGING.value
    return RunOut(**run)


# -- FR7.2 pairwise 对比判决（LLM 旁路，ADR-23 主判决不动） -------------------


class PairwiseBody(BaseModel):
    other_run_id: str


def _run_task_id(run: dict) -> str:
    return str(run.get("task_id") or run.get("eval_task_id") or "")


def _run_events(request: Request, traces: dict, run_id: str) -> list[dict]:
    """事件来源与 traces 端点同路：内存 traces store 优先，回退 collector。"""
    if traces.get(run_id):
        return sorted(traces[run_id], key=lambda e: e.get("seq", 0))
    from rsi.api.routers.traces import _collector_events

    return sorted(_collector_events(request, run_id), key=lambda e: e.get("seq", 0))


def _task_context(stores: dict, task_id: str) -> dict:
    """Run → task 上下文：evalsets store 内嵌任务优先，缺失给最小诚实上下文。

    期望答案取不到时 expected 置空 dict——对比仍在两份轨迹证据上进行，
    只是不再叠加标准答案参照（诚实降级，不编造期望）。
    """
    for es in (stores.get("evalsets") or {}).values():
        for t in es.get("tasks") or []:
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or t.get("task_id")
            if task_id and str(tid or "") == task_id:
                return {
                    "task_id": task_id,
                    "title": str(t.get("title") or task_id),
                    "expected": t.get("expected") or {},
                }
    return {"task_id": task_id, "title": task_id, "expected": {}}


def _antihack_signal_text(
    services: dict, task: dict, events: list, result: Any, run_status: str | None = None
) -> str:
    """AntiHackDetector 信号拼接；detector 未装配或适配失败 → 空串。

    store/collector 里的事件与 runs 记录是 JSON dict，而 ``detect`` 期望
    EvalTask/TraceEvent/TaskResult 对象——按 detect 实际访问的字段做只读
    垫片（task.task.expected.critical_steps[].evidence、event_type/data、
    result.instance_crashed），非 stub：skipped_steps/missing_trial 两类
    刷分信号在真实数据上可产出（FR7.2，B6 验收依赖）。
    信号属增强项：任何失败都不阻断对比（空串即"无附加信号"）。
    """
    detector = services.get("antihack")
    if detector is None or not events:
        return ""
    try:
        from types import SimpleNamespace

        from rsi.core.models import PathQuality
        from rsi.judge.deterministic import DetScores
        from rsi.judge.llm_judge import SemanticScores

        expected = (task.get("expected") or {}) if isinstance(task, dict) else {}
        steps = [
            SimpleNamespace(
                evidence=str(s.get("evidence") or "") if isinstance(s, dict) else str(s)
            )
            for s in (expected.get("critical_steps") or [])
            if s
        ]
        evs = [
            e
            if not isinstance(e, dict)
            else SimpleNamespace(
                # collector 产 event_type；内存 traces store 种子常见 type
                event_type=e.get("event_type") or e.get("type"),
                data=e.get("data") or {},
            )
            for e in events
        ]
        res = (
            SimpleNamespace(instance_crashed=bool(result.get("instance_crashed")))
            if isinstance(result, dict)
            else result
        )
        sem = SemanticScores(
            # 旁路不做语义打分：mismatch 维度诚实置 False（结构上恒 False）
            root_cause_match=None,
            path_quality=PathQuality(verdict="pass", reasoning="pairwise-sideband"),
        )
        sig = detector.detect(
            SimpleNamespace(task=SimpleNamespace(expected=SimpleNamespace(critical_steps=steps))),
            evs,
            DetScores(),
            sem,
            result=res,
            run_status=run_status,
        )
        return "; ".join(
            [
                "skipped_steps=" + (",".join(sig.skipped_steps) or "none"),
                f"missing_trial={str(sig.missing_trial).lower()}",
                "conclusion_process_mismatch="
                + str(sig.conclusion_process_mismatch).lower(),
            ]
        )
    except Exception:  # noqa: BLE001 - 增强信号失败不阻断对比
        return ""


@router.post("/{run_id}/pairwise", response_model=PairwiseReport)
@audit.audited("run.pairwise", object_type="run")
async def pairwise_compare(
    run_id: str,
    body: PairwiseBody,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("runs")),
    traces: dict = Depends(get_store("traces")),
) -> PairwiseReport:
    """FR7.2：同任务两 run 轨迹并排 LLM 对比判决（DD-08 pairwise）。

    与 ``POST /judgments:pairwise``（P0 分数启发式）互补：本端点走
    judge/pairwise 模板三维度语义比对 + 刷分嫌疑标记，不改 ADR-23
    单 run 主判决路径。跨任务/事件缺失 → 422；LLM 失败 → 503。
    """
    run = _get_anywhere(request, store, run_id)
    other = _get_anywhere(request, store, body.other_run_id)
    task_id = _run_task_id(run)
    if task_id != _run_task_id(other):
        raise ValidationError(
            "runs belong to different eval tasks; pairwise comparison "
            "requires the same eval_task_id",
            context={
                "run_id": run_id,
                "other_run_id": body.other_run_id,
                "task_id": task_id,
                "other_task_id": _run_task_id(other),
            },
        )
    events_a = _run_events(request, traces, run_id)
    events_b = _run_events(request, traces, body.other_run_id)
    if not events_a or not events_b:
        missing = run_id if not events_a else body.other_run_id
        raise ValidationError(
            f"no trace events available for run {missing!r}; pairwise "
            "comparison needs both trajectories",
            context={
                "run_id": run_id,
                "other_run_id": body.other_run_id,
                "events_a": len(events_a),
                "events_b": len(events_b),
            },
        )
    judge = request.app.state.services.get("llm_judge")
    if judge is None:
        raise ConfigError(
            "LLM judge not configured: runs pairwise unavailable",
            context={"missing_service": "llm_judge", "run_id": run_id},
        )
    task_ctx = _task_context(request.app.state.stores, task_id)
    signals = "; ".join(
        filter(
            None,
            [
                _antihack_signal_text(
                    request.app.state.services,
                    task_ctx,
                    events_a,
                    run.get("result"),
                    run_status=run.get("status"),
                ),
                _antihack_signal_text(
                    request.app.state.services,
                    task_ctx,
                    events_b,
                    other.get("result"),
                    run_status=other.get("status"),
                ),
            ],
        )
    )
    try:
        report = await judge.pairwise(
            task_ctx,
            (run.get("result"), events_a),
            (other.get("result"), events_b),
            antihack_signals=signals,
        )
    except LLMOutputInvalid as exc:
        # 端点语义：对比失败 = 服务当前不可用（503），错误摘要透出。
        # 不改全局 EXCEPTION_MAP（LLMOutputInvalid 在 judge 域仍是 502）。
        summary = str(exc.context.get("last_error") or exc)
        raise ConfigError(
            f"LLM pairwise comparison invalid after retries (judge/pairwise): "
            f"{summary}",
            context={
                "component": "judge",
                "prompt": "pairwise",
                "run_id": run_id,
                "other_run_id": body.other_run_id,
                "last_error": summary[:500],
            },
        ) from exc
    return report
