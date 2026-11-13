"""Batch endpoints (DD-13 §9.6). P0: in-memory store + optional batch_runner."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.db_reads import list_runs_for_batch
from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.enums import BatchStatus
from rsi.core.errors import InvalidTransitionError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/batches", tags=["batches"])

_TERMINAL = {BatchStatus.FINALIZED, BatchStatus.FAILED, BatchStatus.ABORTED}


class BatchCreate(BaseModel):
    evalset_id: str = Field(json_schema_extra={"example": "es-abc12345"})
    sut_id: str = Field(json_schema_extra={"example": "sut-1a2b3c4d"})
    purpose: Literal["iterate", "holdout", "smoke", "compare"] = Field(
        json_schema_extra={"example": "iterate"}
    )
    profile_id: str | None = Field(
        default=None, json_schema_extra={"example": "prof-9f8e7d6c"}
    )
    params: dict[str, Any] = Field(
        default={},
        json_schema_extra={"example": {"max_parallel": 4, "timeout_s": 1800}},
    )
    # F2/W3：API 模式批-会话关联（引擎路径自行写入；此处供运营者手动关联）
    iteration_id: str | None = Field(default=None)


class BatchOut(BaseModel):
    id: str
    evalset_id: str
    sut_id: str
    purpose: str
    profile_id: str | None
    iteration_id: str | None = None
    status: str
    paused: bool
    progress: dict[str, Any]
    created_at: str


class RunRef(BaseModel):
    id: str
    batch_id: str
    task_id: str | None
    status: str


class RunPage(BaseModel):
    items: list[RunRef]
    next_cursor: str | None = None
    has_more: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, batch_id: str) -> dict:
    try:
        return store[batch_id]
    except KeyError:
        raise NotFoundError(
            f"batch {batch_id!r} not found", context={"batch_id": batch_id}
        ) from None


@router.post("", response_model=BatchOut, status_code=201)
@audit.audited("batch.trigger", object_type="batch")
async def trigger_batch(
    body: BatchCreate,
    request: Request,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("batches")),
    suts: dict = Depends(get_store("suts")),
    evalsets: dict = Depends(get_store("evalsets")),
) -> BatchOut:
    if body.sut_id not in suts:
        raise NotFoundError(
            f"sut {body.sut_id!r} not found", context={"sut_id": body.sut_id}
        )
    if body.evalset_id not in evalsets:
        raise NotFoundError(
            f"evalset {body.evalset_id!r} not found",
            context={"evalset_id": body.evalset_id},
        )
    batch = {
        "id": f"batch-{uuid.uuid4().hex[:8]}",
        "evalset_id": body.evalset_id,
        "sut_id": body.sut_id,
        "purpose": body.purpose,
        "profile_id": body.profile_id,
        "status": BatchStatus.CREATED.value,
        "paused": False,
        "progress": {"done": 0, "total": 0},
        "params": body.params,
        "iteration_id": body.iteration_id,
        "created_at": _now(),
    }
    store[batch["id"]] = batch
    runner = request.app.state.services.get("batch_runner")
    if runner is not None:
        # audit3 W2: the runner only writes the DB, so give it an
        # on_update callback that syncs this in-memory batch dict when the
        # background run finishes (success/failure/abort) — without it the
        # store entry would stay "created" forever.
        trigger = getattr(runner, "trigger", None)
        if trigger is not None:
            try:
                trigger(batch, on_update=lambda upd: batch.update(upd))
            except TypeError:
                # legacy runner trigger() without the on_update kwarg
                trigger(batch)
    return BatchOut(**batch)


@router.get("/{batch_id}", response_model=BatchOut)
async def get_batch(
    batch_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("batches")),
) -> BatchOut:
    return BatchOut(**_get(store, batch_id))


@router.get("/{batch_id}/runs", response_model=RunPage)
async def list_batch_runs(
    batch_id: str,
    request: Request,
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
) -> RunPage:
    _get(store, batch_id)
    # audit4 W-C: production runs live only in the DB; union memory hits
    # (test rigs) with DB rows so the listing is non-empty after a real
    # trigger even though the runner never writes the memory store.
    in_batch = list_runs_for_batch(request, runs, batch_id)
    items, page = paginate(in_batch, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return RunPage(
        items=[RunRef(**r) for r in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


def _transition(batch: dict, action: str) -> None:
    """Guard: raise when the batch is in a terminal state."""
    status = BatchStatus(batch["status"])
    if status in _TERMINAL:
        raise InvalidTransitionError(
            f"cannot {action} batch in terminal state {status.value}",
            context={"batch_id": batch["id"], "from_state": status.value},
        )


@router.post("/{batch_id}:pause", response_model=BatchOut)
@audit.audited("batch.pause", object_type="batch")
async def pause_batch(
    batch_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("batches")),
) -> BatchOut:
    batch = _get(store, batch_id)
    _transition(batch, "pause")
    if batch["status"] != BatchStatus.RUNNING.value:
        raise InvalidTransitionError(
            f"cannot pause batch in state {batch['status']}",
            context={"batch_id": batch_id, "from_state": batch["status"]},
        )
    batch["paused"] = True
    return BatchOut(**batch)


@router.post("/{batch_id}:resume", response_model=BatchOut)
@audit.audited("batch.resume", object_type="batch")
async def resume_batch(
    batch_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("batches")),
) -> BatchOut:
    batch = _get(store, batch_id)
    _transition(batch, "resume")
    if not batch["paused"]:
        raise InvalidTransitionError(
            "batch is not paused",
            context={"batch_id": batch_id, "from_state": batch["status"]},
        )
    batch["paused"] = False
    return BatchOut(**batch)


@router.post("/{batch_id}:abort", response_model=BatchOut)
@audit.audited("batch.abort", object_type="batch")
async def abort_batch(
    batch_id: str,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("batches")),
) -> BatchOut:
    batch = _get(store, batch_id)
    _transition(batch, "abort")
    batch["status"] = BatchStatus.ABORTED.value
    batch["paused"] = False
    return BatchOut(**batch)


# ── 列表 + :rerun（interfaces §9.6，FR9.1 重测）──────────────────────────

class BatchPage(BaseModel):
    items: list[BatchOut]
    next_cursor: str | None = None
    has_more: bool = False


class RerunBody(BaseModel):
    scope: Literal["failed", "all"] = "failed"
    bypass_cache: bool = True


@router.get("", response_model=BatchPage)
async def list_batches(
    response: Response,
    sut_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("batches")),
) -> BatchPage:
    """批次列表（?sut_id=&status= 过滤，游标分页）。"""
    items = list(store.values())
    if sut_id is not None:
        items = [b for b in items if b["sut_id"] == sut_id]
    if status is not None:
        items = [b for b in items if b["status"] == status]
    page_items, page = paginate(items, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return BatchPage(
        items=[BatchOut(**b) for b in page_items],
        next_cursor=page["next_cursor"],
        has_more=page["has_more"],
    )


@router.post("/{batch_id}:rerun", status_code=204)
@audit.audited("batch.rerun", object_type="batch")
async def rerun_batch(
    batch_id: str,
    body: RerunBody,
    request: Request,
    user: Claims = Depends(require_perms("batch:trigger")),
    store: dict = Depends(get_store("batches")),
) -> Response:
    """重测（FR9.1）：复制原批次 purpose 构造新批次并以 bypass_cache 重跑 → 204。

    rerun 批次不引入新 purpose 值：purpose 复制原批次，重测来源与范围由
    ``params.rerun_of``（原批 id）与 ``params.rerun_scope``（failed|all）标记。

    批次仍在 created/running（无可重测对象）→ 409。BatchRunner 的缓存旁路
    改造属 F4 批次；此处把 ``bypass_cache``/``rerun_scope`` 写入批次记录并
    传递给 runner（若其 trigger/run_batch 签名支持）。
    """
    batch = _get(store, batch_id)
    if batch["status"] in (BatchStatus.CREATED.value, BatchStatus.RUNNING.value):
        raise InvalidTransitionError(
            f"batch {batch_id!r} is {batch['status']} (nothing to rerun)",
            context={"batch_id": batch_id, "from_state": batch["status"]},
        )
    rerun = {
        "id": f"batch-{uuid.uuid4().hex[:8]}",
        "evalset_id": batch["evalset_id"],
        "sut_id": batch["sut_id"],
        "purpose": batch["purpose"],
        "profile_id": batch.get("profile_id"),
        "status": BatchStatus.CREATED.value,
        "paused": False,
        "progress": {"done": 0, "total": 0},
        "params": {
            **dict(batch.get("params", {})),
            "rerun_of": batch_id,
            "rerun_scope": body.scope,
            "bypass_cache": True,
        },
        "created_at": _now(),
    }
    runner = request.app.state.services.get("batch_runner")
    if runner is not None:
        # D-4: bypass is per-call — a rerun batch skips cache reads AND
        # writes without flipping the sticky runner-level default (which
        # would silently disable cache hits for every later batch).
        trigger = getattr(runner, "trigger", None)
        if trigger is not None:
            try:
                trigger(
                    rerun,
                    bypass_cache=body.bypass_cache,
                    on_update=lambda upd: rerun.update(upd),
                )
            except TypeError:
                try:
                    trigger(rerun, bypass_cache=body.bypass_cache)
                except TypeError:
                    # legacy runner trigger() without the per-call bypass kwarg
                    trigger(rerun)
    store[rerun["id"]] = rerun
    return Response(status_code=204)
