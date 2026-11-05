"""SUT container instance endpoints (DD-13). P0: in-memory store."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/instances", tags=["instances"])


class InstanceOut(BaseModel):
    id: str
    sut_id: str
    lab_id: str | None
    status: str


class InstancePage(BaseModel):
    items: list[InstanceOut]


@router.get("", response_model=InstancePage)
async def list_instances(
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    sut_id: str | None = None,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("instances")),
) -> InstancePage:
    items = [i for i in store.values() if sut_id is None or i["sut_id"] == sut_id]
    page_items, _page = paginate(items, cursor, limit)
    return InstancePage(items=[InstanceOut(**i) for i in page_items])


@router.delete("/{instance_id}", status_code=204)
@audit.audited("instance.cleanup", object_type="instance")
async def delete_instance(
    instance_id: str,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("instances")),
) -> Response:
    try:
        inst = store[instance_id]
    except KeyError:
        raise NotFoundError(
            f"instance {instance_id!r} not found",
            context={"instance_id": instance_id},
        ) from None
    inst["status"] = "destroyed"
    del store[instance_id]
    return Response(status_code=204)
