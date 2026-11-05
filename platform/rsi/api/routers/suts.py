"""SUT registry endpoints (DD-13 §9.2). P0: in-memory store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/suts", tags=["suts"])


class SutCreate(BaseModel):
    name: str
    carrier: str
    image_ref: str
    description: str = ""


class SutUpdate(BaseModel):
    name: str | None = None
    carrier: str | None = None
    image_ref: str | None = None
    description: str | None = None


class SutOut(BaseModel):
    id: str
    name: str
    carrier: str
    image_ref: str
    description: str
    created_at: str


class SutPage(BaseModel):
    items: list[SutOut]
    next_cursor: str | None = None
    has_more: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, sut_id: str) -> dict:
    try:
        return store[sut_id]
    except KeyError:
        raise NotFoundError(f"sut {sut_id!r} not found", context={"sut_id": sut_id}) from None


@router.get("", response_model=SutPage)
async def list_suts(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("suts")),
) -> SutPage:
    items, page = paginate(list(store.values()), cursor, limit)
    response.headers["x-pagination"] = _page_json(page)
    return SutPage(
        items=[SutOut(**i) for i in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


@router.post("", response_model=SutOut, status_code=201)
@audit.audited("sut.create", object_type="sut")
async def create_sut(
    body: SutCreate,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    sut = {
        "id": f"sut-{uuid.uuid4().hex[:8]}",
        "name": body.name,
        "carrier": body.carrier,
        "image_ref": body.image_ref,
        "description": body.description,
        "created_at": _now(),
    }
    store[sut["id"]] = sut
    return SutOut(**sut)


@router.get("/{sut_id}", response_model=SutOut)
async def get_sut(
    sut_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    return SutOut(**_get(store, sut_id))


@router.put("/{sut_id}", response_model=SutOut)
@audit.audited("sut.update", object_type="sut")
async def update_sut(
    sut_id: str,
    body: SutUpdate,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    sut = _get(store, sut_id)
    updates = body.model_dump(exclude_unset=True)
    sut.update(updates)
    return SutOut(**sut)


@router.delete("/{sut_id}", status_code=204)
@audit.audited("sut.delete", object_type="sut")
async def delete_sut(
    sut_id: str,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    _get(store, sut_id)
    del store[sut_id]
    return Response(status_code=204)


def _page_json(page: dict) -> str:
    return json.dumps(page)
