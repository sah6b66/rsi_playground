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
from rsi.core.errors import InvalidTransitionError, NotFoundError
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
    events = sorted(traces.get(run_id, []), key=lambda e: e["seq"])
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
