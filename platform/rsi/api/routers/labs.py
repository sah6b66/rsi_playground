"""Lab (sandbox topology) endpoints (DD-13). P0: in-memory store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.enums import LabStatus
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

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
    user: Claims = Depends(get_current_user),
) -> dict:
    return {
        "items": [
            {"name": "docker", "status": "ready"},
            {"name": "mock", "status": "ready"},
        ]
    }
