"""Carrier execution-host endpoints (DD-14 §5.9, T6)：GET/PUT /carrier/hosts。

GET 聚合 settings.carrier.hosts（静态配置）与 HostPool 运行态（在线/
实例计数）；PUT 写 settings.yaml ``carrier.hosts`` 段（静态项语义——
返回 restart_required，运行中 HostPool 不热重建）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from rsi.api.deps import audit, require_perms
from rsi.security.auth import Claims
from rsi.settings import write_settings_section

router = APIRouter(prefix="/carrier", tags=["carrier-hosts"])


class CarrierHostItem(BaseModel):
    key: str = Field(json_schema_extra={"example": "remote-1"})
    mode: Literal["local", "remote"] = "local"
    endpoint: str = Field(default="local", json_schema_extra={"example": "ssh://user@192.168.1.50"})
    online: bool = False
    active_instances: int = 0
    max_instances: int = 2
    subnets: list[str] = Field(default_factory=list)


class CarrierHostsPage(BaseModel):
    items: list[CarrierHostItem]


class CarrierHostItemIn(BaseModel):
    """PUT 输入项：endpoint 为 ``local`` 或 ``ssh://user@host[:port]``。"""

    key: str
    endpoint: str = Field(default="local", json_schema_extra={"example": "ssh://user@192.168.1.50"})
    max_instances: int | None = Field(
        default=None, ge=1, le=64, json_schema_extra={"example": 2}
    )
    subnets: list[str] = Field(default_factory=list, json_schema_extra={"example": ["10.101.0.0/16"]})


class CarrierHostsPutBody(BaseModel):
    items: list[CarrierHostItemIn]


@router.get("/hosts", response_model=CarrierHostsPage)
async def list_hosts(
    request: Request,
    user: Claims = Depends(require_perms("view:read")),
) -> CarrierHostsPage:
    settings = request.app.state.services.get("settings")
    pool = request.app.state.services.get("host_pool")
    statuses = {s.host: s for s in pool.statuses()} if pool is not None else {}
    items: list[CarrierHostItem] = []
    seen: set[str] = set()
    if settings is not None:
        for key, cfg in settings.carrier.hosts.items():
            seen.add(key)
            st = statuses.get(key)
            items.append(
                CarrierHostItem(
                    key=key,
                    mode="remote" if str(cfg.docker).startswith("ssh") else "local",
                    endpoint=cfg.docker,
                    online=bool(st.reachable) if st is not None else False,
                    active_instances=st.instance_count if st is not None else 0,
                    max_instances=cfg.max_instances
                    or settings.carrier.max_instances_per_host,
                    subnets=list(cfg.subnets),
                )
            )
    # HostPool 中存在但 settings 未登记的主机（如运行期注入）也列出
    for key, st in statuses.items():
        if key in seen:
            continue
        items.append(
            CarrierHostItem(
                key=key,
                mode="remote" if str(st.docker_url).startswith("ssh") else "local",
                endpoint=st.docker_url,
                online=bool(st.reachable),
                active_instances=st.instance_count,
                max_instances=st.max_instances or 2,
                subnets=[],
            )
        )
    return CarrierHostsPage(items=items)


@router.put("/hosts")
@audit.audited("carrier.hosts_update", object_type="settings")
async def put_hosts(
    body: CarrierHostsPutBody,
    request: Request,
    user: Claims = Depends(require_perms("env:manage")),
) -> dict:
    """整段重写 settings.yaml ``carrier.hosts`` → {restart_required: true}。"""
    data: dict[str, dict] = {}
    for it in body.items:
        entry: dict = {"docker": it.endpoint or "local", "subnets": it.subnets}
        if it.max_instances is not None:
            entry["max_instances"] = it.max_instances
        data[it.key] = entry
    settings = request.app.state.services.get("settings")
    write_settings_section(
        "carrier.hosts", data, home=getattr(settings, "home", None)
    )
    return {"restart_required": True}
