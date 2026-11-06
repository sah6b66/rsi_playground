"""SUT container instance endpoints (DD-13). P0: in-memory store."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
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
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("instances")),
) -> Response:
    """清理残留实例 → 204。优先经 HostService.destroy（真实清理：
    撤网 / 释放 host slot / 停止跟踪）；无服务时仅清内存 store。"""
    try:
        inst = store[instance_id]
    except KeyError:
        raise NotFoundError(
            f"instance {instance_id!r} not found",
            context={"instance_id": instance_id},
        ) from None
    host_service = request.app.state.services.get("host_service")
    if host_service is not None:
        from rsi.carrier.service import InstanceHandle

        host_service.destroy(
            InstanceHandle(
                instance_id=instance_id,
                sut_id=inst.get("sut_id", ""),
                run_id=inst.get("run_id", ""),
                host=inst.get("host", ""),
                container_id=inst.get("container_id", ""),
                status=inst.get("status", "running"),
                config_dir=Path(inst.get("config_dir", ".")),
            )
        )
    inst["status"] = "destroyed"
    del store[instance_id]
    return Response(status_code=204)
