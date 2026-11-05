"""Eval-set endpoints (DD-13 §9.4). P0: in-memory store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import ConflictError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/evalsets", tags=["evalsets"])


class EvalsetCreate(BaseModel):
    name: str
    tasks: list[dict[str, Any]] = []
    description: str = ""


class EvalsetUpdate(BaseModel):
    name: str | None = None
    tasks: list[dict[str, Any]] | None = None
    description: str | None = None


class EvalsetOut(BaseModel):
    id: str
    name: str
    tasks: list[dict[str, Any]]
    description: str
    frozen: bool
    version: int
    created_at: str


class EvalsetPage(BaseModel):
    items: list[EvalsetOut]
    next_cursor: str | None = None
    has_more: bool = False


class VersionOut(BaseModel):
    version: int
    frozen: bool
    created_at: str


class VersionPage(BaseModel):
    items: list[VersionOut]
    next_cursor: str | None = None
    has_more: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, evalset_id: str) -> dict:
    try:
        return store[evalset_id]
    except KeyError:
        raise NotFoundError(
            f"evalset {evalset_id!r} not found", context={"evalset_id": evalset_id}
        ) from None


@router.get("", response_model=EvalsetPage)
async def list_evalsets(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetPage:
    items, page = paginate(list(store.values()), cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return EvalsetPage(
        items=[EvalsetOut(**i) for i in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


@router.post("", response_model=EvalsetOut, status_code=201)
@audit.audited("evalset.create", object_type="evalset")
async def create_evalset(
    body: EvalsetCreate,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetOut:
    es = {
        "id": f"es-{uuid.uuid4().hex[:8]}",
        "name": body.name,
        "tasks": body.tasks,
        "description": body.description,
        "frozen": False,
        "version": 1,
        "created_at": _now(),
        "versions": [],
    }
    store[es["id"]] = es
    return EvalsetOut(**es)


@router.get("/{evalset_id}", response_model=EvalsetOut)
async def get_evalset(
    evalset_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetOut:
    return EvalsetOut(**_get(store, evalset_id))


@router.put("/{evalset_id}", response_model=EvalsetOut)
@audit.audited("evalset.update", object_type="evalset")
async def update_evalset(
    evalset_id: str,
    body: EvalsetUpdate,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetOut:
    es = _get(store, evalset_id)
    if es["frozen"]:
        raise ConflictError(
            f"evalset {evalset_id!r} is frozen",
            context={"evalset_id": evalset_id},
        )
    updates = body.model_dump(exclude_unset=True)
    if updates:
        es.update(updates)
        es["version"] += 1
    return EvalsetOut(**es)


@router.delete("/{evalset_id}", status_code=204)
@audit.audited("evalset.delete", object_type="evalset")
async def delete_evalset(
    evalset_id: str,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> Response:
    _get(store, evalset_id)
    del store[evalset_id]
    return Response(status_code=204)


@router.post("/{evalset_id}:freeze", response_model=EvalsetOut)
@audit.audited("evalset.freeze", object_type="evalset")
async def freeze_evalset(
    evalset_id: str,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetOut:
    es = _get(store, evalset_id)
    if es["frozen"]:
        raise ConflictError(
            f"evalset {evalset_id!r} already frozen",
            context={"evalset_id": evalset_id},
        )
    es["frozen"] = True
    es["versions"].append(
        {"version": es["version"], "frozen": True, "created_at": _now()}
    )
    return EvalsetOut(**es)


@router.get("/{evalset_id}/versions", response_model=VersionPage)
async def list_versions(
    evalset_id: str,
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("evalsets")),
) -> VersionPage:
    es = _get(store, evalset_id)
    items, page = paginate(es["versions"], cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return VersionPage(
        items=[VersionOut(**v) for v in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )
