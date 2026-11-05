"""Batch endpoints (DD-13 §9.6). P0: in-memory store + optional batch_runner."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

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


class BatchOut(BaseModel):
    id: str
    evalset_id: str
    sut_id: str
    purpose: str
    profile_id: str | None
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
        "created_at": _now(),
    }
    runner = request.app.state.services.get("batch_runner")
    if runner is not None:
        runner.trigger(batch)
    store[batch["id"]] = batch
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
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("batches")),
    runs: dict = Depends(get_store("runs")),
) -> RunPage:
    _get(store, batch_id)
    in_batch = [r for r in runs.values() if r["batch_id"] == batch_id]
    items, page = paginate(in_batch, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return RunPage(
        items=[RunRef(**r) for r in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


def _transition(batch: dict, action: str) -> dict:
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
