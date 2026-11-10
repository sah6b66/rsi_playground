"""Lab (sandbox topology) endpoints (DD-13). P0: in-memory store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.enums import LabStatus
from rsi.core.errors import ConflictError, NotFoundError
from rsi.security.auth import Claims
from rsi.settings import write_settings_section

router = APIRouter(tags=["labs"])


class LabCreate(BaseModel):
    sut_id: str
    topology: dict[str, Any] = {}


class LabOut(BaseModel):
    id: str
    sut_id: str
    status: str
    online: bool
    topology: dict[str, Any]
    created_at: str


class LabPage(BaseModel):
    items: list[LabOut]


class TopologyOut(BaseModel):
    lab_id: str
    nodes: list[dict[str, Any]]
    links: list[dict[str, Any]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, lab_id: str) -> dict:
    try:
        return store[lab_id]
    except KeyError:
        raise NotFoundError(
            f"lab {lab_id!r} not found", context={"lab_id": lab_id}
        ) from None


def _default_topology(sut_id: str) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "sut", "kind": "container", "ref": sut_id},
            {"id": "net", "kind": "network", "ref": "default"},
        ],
        "links": [{"from": "sut", "to": "net"}],
    }


@router.get("/labs", response_model=LabPage)
async def list_labs(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("labs")),
) -> LabPage:
    labs = sorted(store.values(), key=lambda l: l["created_at"])
    items, page = paginate(labs, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return LabPage(items=[LabOut(**l) for l in items])


@router.post("/labs", response_model=LabOut, status_code=201)
@audit.audited("lab.create", object_type="lab")
async def create_lab(
    body: LabCreate,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("labs")),
) -> LabOut:
    topo = dict(body.topology)
    topo.setdefault("nodes", _default_topology(body.sut_id)["nodes"])
    topo.setdefault("links", _default_topology(body.sut_id)["links"])
    lab = {
        "id": f"lab-{uuid.uuid4().hex[:8]}",
        "sut_id": body.sut_id,
        "status": LabStatus.READY.value,
        "online": True,
        "topology": topo,
        "created_at": _now(),
    }
    store[lab["id"]] = lab
    return LabOut(**lab)


@router.get("/labs/{lab_id}", response_model=LabOut)
async def get_lab(
    lab_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("labs")),
) -> LabOut:
    return LabOut(**_get(store, lab_id))


@router.get("/labs/{lab_id}/topology", response_model=TopologyOut)
async def get_lab_topology(
    lab_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("labs")),
) -> TopologyOut:
    lab = _get(store, lab_id)
    topo = lab["topology"]
    return TopologyOut(
        lab_id=lab_id,
        nodes=list(topo.get("nodes", [])),
        links=list(topo.get("links", [])),
    )


@router.delete("/labs/{lab_id}", status_code=204)
@audit.audited("lab.destroy", object_type="lab")
async def delete_lab(
    lab_id: str,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("labs")),
) -> Response:
    _get(store, lab_id)
    del store[lab_id]
    return Response(status_code=204)


@router.post("/labs/{lab_id}/reset", response_model=LabOut)
@audit.audited("lab.reset", object_type="lab")
async def reset_lab(
    lab_id: str,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("labs")),
) -> LabOut:
    lab = _get(store, lab_id)
    lab["status"] = LabStatus.READY.value
    lab["online"] = True
    lab["topology"] = _default_topology(lab["sut_id"])
    return LabOut(**lab)


@router.get("/sandbox/backends")
async def list_backends(
    request: Request,
    user: Claims = Depends(get_current_user),
) -> dict:
    """Aggregated real backend availability (T4); placeholder fallback when
    no sandbox service is wired (e.g. minimal app assemblies)."""
    services = getattr(request.app.state, "services", None) or {}
    svc = services.get("sandbox_service")
    if svc is not None:
        statuses = await svc.backend_status()
        # 平台级设备清单计数（settings 静态段；监控寻址与在用 lab 无关）
        cfg_backends = getattr(getattr(services.get("settings"), "sandbox", None), "backends", {}) or {}
        def _cfg_devices(name: str) -> dict:
            return getattr(cfg_backends.get(name), "devices", None) or {}

        return {
            "items": [
                {
                    "name": s.key,
                    "kind": s.kind,
                    "status": "ready" if s.available else "offline",
                    "available": s.available,
                    "active_labs": s.active_labs,
                    "detail": s.detail,
                    "device_count": len(_cfg_devices(s.key)),
                    "devices": {
                        n: {"ip": ep.ip, "port": ep.port}
                        for n, ep in _cfg_devices(s.key).items()
                    },
                }
                for s in statuses
            ]
        }
    return {
        "items": [
            {"name": "docker", "status": "ready"},
            {"name": "mock", "status": "ready"},
        ]
    }


class DeviceEndpointPut(BaseModel):
    """平台级设备端点（监控寻址用 ip:port；凭证属评测集 env.yaml）。"""

    ip: str
    port: int = Field(22, ge=1, le=65535)


class BackendPutBody(BaseModel):
    """PUT /sandbox/backends/{key}：写 settings.yaml 静态段（DD-14 §5.8）。"""

    kind: Literal["ensp-pro", "pnetlab", "physical", "fake"]
    url: str = ""
    verify_tls: bool = False
    subnets: list[str] = []
    devices: dict[str, DeviceEndpointPut] = {}


@router.put("/sandbox/backends/{key}")
@audit.audited("sandbox.backend_update", object_type="settings")
async def put_backend(
    key: str,
    body: BackendPutBody,
    request: Request,
    user: Claims = Depends(require_perms("sandbox:manage")),
) -> dict:
    """编辑/新增沙箱平台 → 写 settings.yaml ``sandbox.backends.<key>``。

    静态项语义：运行中的 SandboxService 不热重建，返回 restart_required。
    """
    services = getattr(request.app.state, "services", None) or {}
    settings = services.get("settings")
    # 空 devices → 不写入（section 整体替换，旧清单即被清除）
    payload: dict[str, Any] = {
        "kind": body.kind,
        "url": body.url,
        "verify_tls": body.verify_tls,
        "subnets": body.subnets,
    }
    if body.devices:
        payload["devices"] = {
            name: {"ip": ep.ip, "port": ep.port}
            for name, ep in body.devices.items()
        }
    write_settings_section(
        f"sandbox.backends.{key}",
        payload,
        home=getattr(settings, "home", None),
    )
    return {"restart_required": True}


# ── snapshots / restore / readiness / exec（interfaces §9.13）────────────

class SnapshotBody(BaseModel):
    name: str = ""


class SnapshotOut(BaseModel):
    snapshot_id: str


class RestoreBody(BaseModel):
    snapshot_id: str


class ExecBody(BaseModel):
    cli: str


def _device_present(lab: dict, device: str) -> bool:
    for node in lab.get("topology", {}).get("nodes", []):
        if node.get("id") == device or node.get("name") == device:
            return True
    return False


def _sandbox(request: Request) -> Any:
    return request.app.state.services.get("sandbox_service")


def _lab_handle(lab: dict) -> Any:
    return lab.get("handle")


@router.post("/labs/{lab_id}/snapshots", response_model=SnapshotOut, status_code=201)
@audit.audited("lab.snapshot", object_type="lab")
async def snapshot_lab(
    lab_id: str,
    body: SnapshotBody,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("labs")),
) -> SnapshotOut:
    """手动快照 → 201{snapshot_id}。

    lab 记录携带 ``handle``（LabHandle）且 sandbox 服务可用时走真实后端；
    否则 P0 内存登记（文档未定义 P0 行为，实现取内存快照）。
    """
    lab = _get(store, lab_id)
    sandbox = _sandbox(request)
    handle = _lab_handle(lab)
    if sandbox is not None and handle is not None:
        snap = sandbox.snapshot(handle, body.name or f"snap-{lab_id}")
        snap_id = snap.snapshot_id
    else:
        snap_id = f"snap-{uuid.uuid4().hex[:8]}"
    lab.setdefault("snapshots", []).append(
        {"snapshot_id": snap_id, "name": body.name, "created_at": _now()}
    )
    return SnapshotOut(snapshot_id=snap_id)


@router.post("/labs/{lab_id}/restore", status_code=204)
@audit.audited("lab.restore", object_type="lab")
async def restore_lab(
    lab_id: str,
    body: RestoreBody,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("labs")),
) -> Response:
    """快照恢复 → 204；快照不存在 → 404。"""
    lab = _get(store, lab_id)
    snaps = lab.get("snapshots", [])
    if not any(s["snapshot_id"] == body.snapshot_id for s in snaps):
        raise NotFoundError(
            f"snapshot {body.snapshot_id!r} not found in lab {lab_id!r}",
            context={"lab_id": lab_id, "snapshot_id": body.snapshot_id},
        )
    sandbox = _sandbox(request)
    handle = _lab_handle(lab)
    if sandbox is not None and handle is not None:
        from rsi.core.models import SnapRef

        sandbox.restore(
            handle,
            SnapRef(snapshot_id=body.snapshot_id, impl_kind="native"),
        )
    lab["restored_from"] = body.snapshot_id
    return Response(status_code=204)


@router.get("/labs/{lab_id}/devices/{device}/readiness")
async def device_readiness(
    lab_id: str,
    device: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("labs")),
) -> dict:
    """设备就绪报告 → ReadyReport（P0：拓扑在场性 + 基础检查清单）。"""
    lab = _get(store, lab_id)
    if not _device_present(lab, device):
        raise NotFoundError(
            f"device {device!r} not found in lab {lab_id!r}",
            context={"lab_id": lab_id, "device": device},
        )
    return {
        "lab_id": lab_id,
        "device": device,
        "ready": True,
        "checks": [
            {"item": "present_in_topology", "passed": True, "detail": ""},
            {"item": "mgmt_reachable", "passed": True, "detail": "P0 stub"},
        ],
    }


@router.post("/labs/{lab_id}/devices/{device}/exec")
@audit.audited("lab.exec_readonly", object_type="lab")
async def exec_device(
    lab_id: str,
    device: str,
    body: ExecBody,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("labs")),
) -> Response:
    """只读诊断 exec → 输出（前缀白名单约束，非白名单 409）。

    优先经 SandboxService.exec_readonly（真实执行层）；无服务/handle 时
    仅做白名单前缀校验（DeviceError → 409 由全局映射）。
    """
    lab = _get(store, lab_id)
    if not _device_present(lab, device):
        raise NotFoundError(
            f"device {device!r} not found in lab {lab_id!r}",
            context={"lab_id": lab_id, "device": device},
        )
    sandbox = _sandbox(request)
    handle = _lab_handle(lab)
    if sandbox is not None and handle is not None:
        output = sandbox.exec_readonly(handle, device, body.cli)
        return Response(content=output, media_type="text/plain")
    from rsi.sandbox.service import READONLY_PREFIXES

    if not body.cli.strip().lower().startswith(READONLY_PREFIXES):
        raise ConflictError(
            f"cli {body.cli!r} is not in the read-only whitelist",
            context={"lab_id": lab_id, "device": device, "cli": body.cli},
        )
    return Response(
        content=f"[{lab_id}:{device}] {body.cli}\n(P0 simulated read-only output)",
        media_type="text/plain",
    )
