"""Eval-set endpoints (DD-13 §9.4). P0: in-memory store."""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import ConflictError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/evalsets", tags=["evalsets"])


class EvalsetCreate(BaseModel):
    name: str = Field(json_schema_extra={"example": "netops-bgp-triage"})
    tasks: list[dict[str, Any]] = Field(
        default=[],
        json_schema_extra={
            "example": [
                {"id": "t-001", "prompt": "Diagnose why BGP peer 10.0.0.2 is down"},
                {"id": "t-002", "prompt": "Find the interface with the highest error rate"}
            ],
        },
    )
    description: str = Field(
        default="", json_schema_extra={"example": "BGP troubleshooting eval tasks"}
    )


class EvalsetUpdate(BaseModel):
    name: str | None = Field(default=None, json_schema_extra={"example": "netops-bgp-triage-v2"})
    tasks: list[dict[str, Any]] | None = Field(
        default=None,
        json_schema_extra={"example": [{"id": "t-003", "prompt": "Check OSPF neighbors"}]},
    )
    description: str | None = Field(
        default=None, json_schema_extra={"example": "Updated task set"}
    )


class EvalsetOut(BaseModel):
    id: str
    name: str
    purpose: str = "iterate"
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
    git_ref: str | None = None
    content_hash: str | None = None
    frozen_at: str | None = None
    frozen: bool = True
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
    if any(e["name"] == body.name for e in store.values()):
        raise ConflictError(
            f"evalset name {body.name!r} already exists",
            context={"name": body.name},
        )
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
    content_hash = hashlib.sha256(
        json.dumps(es["tasks"], sort_keys=True).encode()
    ).hexdigest()
    es["content_hash"] = content_hash
    es["frozen_at"] = _now()
    es["versions"].append(
        {
            "version": es["version"],
            "git_ref": None,  # git_ref 由 GitStore.freeze_evalset 提供（文档未定义接入方式，P0 置空）
            "content_hash": content_hash,
            "frozen_at": es["frozen_at"],
            "frozen": True,
            "created_at": _now(),
        }
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


# ── :assign / :export / :import (interfaces §9.4) ─────────────────────────

class AssignBody(BaseModel):
    purpose: Literal["iterate", "holdout", "generalization", "smoke"] = Field(
        json_schema_extra={"example": "holdout"}
    )


@router.post("/{evalset_id}:assign", status_code=204)
@audit.audited("evalset.assign", object_type="evalset")
async def assign_evalset(
    evalset_id: str,
    body: AssignBody,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> Response:
    """用途分配 → 204（purpose 非法值由 pydantic Literal → 422）。"""
    es = _get(store, evalset_id)
    es["purpose"] = body.purpose
    return Response(status_code=204)


def _evalset_zip_bytes(es: dict) -> bytes:
    """三件套原格式 zip（FR3.5）：env.yaml + tasks.jsonl + configs/。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("env.yaml", str(es.get("env_yaml", "")))
        zf.writestr(
            "tasks.jsonl",
            "\n".join(json.dumps(t, ensure_ascii=False) for t in es["tasks"]),
        )
        for rel, content in (es.get("configs") or {}).items():
            zf.writestr(f"configs/{rel}", content)
    return buf.getvalue()


@router.post("/{evalset_id}:export")
async def export_evalset(
    evalset_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("evalsets")),
) -> Response:
    """导出三件套原格式 zip（FR3.5）。"""
    es = _get(store, evalset_id)
    return Response(
        content=_evalset_zip_bytes(es),
        media_type="application/zip",
        headers={
            "content-disposition": f'attachment; filename="{es["name"]}.zip"'
        },
    )


@router.post(":import", response_model=EvalsetOut, status_code=201)
@audit.audited("evalset.import", object_type="evalset")
async def import_evalset(
    file: UploadFile = File(...),
    name: str = Form(""),
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("evalsets")),
) -> EvalsetOut:
    """导入三件套 zip → 201{id}；名冲突 409。

    zip 结构约定（文档未定义细节，实现取 export 的对称格式）：
    ``env.yaml``、``tasks.jsonl``、``configs/**``。
    """
    if name and any(e["name"] == name for e in store.values()):
        raise ConflictError(
            f"evalset name {name!r} already exists", context={"name": name}
        )
    tasks: list[dict[str, Any]] = []
    env_yaml = ""
    configs: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(await file.read())) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            data = zf.read(info.filename).decode("utf-8", "replace")
            if info.filename == "tasks.jsonl":
                tasks = [
                    json.loads(line)
                    for line in data.splitlines()
                    if line.strip()
                ]
            elif info.filename in ("env.yaml", "env.yml"):
                env_yaml = data
            elif info.filename.startswith("configs/"):
                configs[info.filename[len("configs/"):]] = data
    es_name = name or file.filename or "imported"
    if any(e["name"] == es_name for e in store.values()):
        raise ConflictError(
            f"evalset name {es_name!r} already exists", context={"name": es_name}
        )
    es = {
        "id": f"es-{uuid.uuid4().hex[:8]}",
        "name": es_name,
        "purpose": "iterate",
        "tasks": tasks,
        "description": f"imported from {file.filename}",
        "env_yaml": env_yaml,
        "configs": configs,
        "frozen": False,
        "version": 1,
        "created_at": _now(),
        "versions": [],
    }
    store[es["id"]] = es
    return EvalsetOut(**es)
