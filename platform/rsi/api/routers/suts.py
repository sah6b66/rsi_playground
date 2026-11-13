"""SUT registry endpoints (interfaces §9.2). P0: in-memory store + GitStore."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import ConflictError, NotFoundError, ValidationError
from rsi.optimizer.targets import TARGET_PATHS, TargetRegistry
from rsi.security.auth import Claims

router = APIRouter(prefix="/suts", tags=["suts"])


class SutCreate(BaseModel):
    name: str = Field(json_schema_extra={"example": "ipops-agent"})
    carrier: str = Field(json_schema_extra={"example": "opencode"})
    image_ref: str = Field(json_schema_extra={"example": "ipops-agent:v1.2"})
    description: str = Field(
        default="", json_schema_extra={"example": "Network ops SUT for BGP triage"}
    )
    # U7b per-agent optimization scope: SUT-tree-relative path patterns
    # (e.g. "skills/bgp-diag/", "mcp.json", "scripts/"). None/empty =
    # automatic mode (platform scans the agent tree and decides).
    optimization_scope: list[str] | None = Field(
        default=None,
        json_schema_extra={"example": ["skills/bgp-diag/", "mcp.json"]},
    )


class SutUpdate(BaseModel):
    name: str | None = Field(default=None, json_schema_extra={"example": "ipops-agent"})
    carrier: str | None = Field(default=None, json_schema_extra={"example": "opencode"})
    image_ref: str | None = Field(
        default=None, json_schema_extra={"example": "ipops-agent:v1.3"}
    )
    description: str | None = Field(
        default=None, json_schema_extra={"example": "Updated description"}
    )


class SutScopeUpdate(BaseModel):
    optimization_scope: list[str] | None = None


class SutOut(BaseModel):
    id: str
    name: str
    carrier: str
    image_ref: str
    description: str
    created_at: str
    optimization_scope: list[str] | None = None


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


def _validate_scope(sut_id: str, scope: list[str] | None) -> list[str] | None:
    """U7b: each scope item must sit inside one of the 8 global whitelist
    domains (checked with the same TargetRegistry the executor layer uses —
    the scope can only *narrow* the global whitelist, never widen it).

    Returns the normalized scope (empty list normalized to None = automatic
    mode); raises ValidationError (422) listing the offending items.
    """
    if not scope:
        return None
    invalid: list[str] = []
    for item in scope:
        rel = str(item)
        try:
            _target_registry.validate(f"suts/{sut_id}/{rel}", _ALL_TARGETS)
        except Exception:  # noqa: BLE001 - any rejection marks the item invalid
            invalid.append(rel)
    if invalid:
        raise ValidationError(
            "optimization_scope 项超出全局优化白名单域: "
            + ", ".join(repr(i) for i in invalid),
            context={"sut_id": sut_id, "invalid": invalid},
        )
    return list(scope)


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
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    if any(s["name"] == body.name for s in store.values()):
        raise ConflictError(
            f"sut name {body.name!r} already exists", context={"name": body.name}
        )
    sut_id = f"sut-{uuid.uuid4().hex[:8]}"
    sut = {
        "id": sut_id,
        "name": body.name,
        "carrier": body.carrier,
        "image_ref": body.image_ref,
        "description": body.description,
        "created_at": _now(),
        # U7b: explicit per-agent RSI-optimizable paths (None = automatic).
        "optimization_scope": _validate_scope(sut_id, body.optimization_scope),
    }
    store[sut["id"]] = sut
    from rsi.api.object_store import persist_record

    persist_record(request.app.state.services, "suts", sut)
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
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    sut = _get(store, sut_id)
    updates = body.model_dump(exclude_unset=True)
    sut.update(updates)
    from rsi.api.object_store import persist_record

    persist_record(request.app.state.services, "suts", sut)
    return SutOut(**sut)


@router.put("/{sut_id}/optimization-scope", response_model=SutOut)
@audit.audited("sut.update", object_type="sut")
async def update_sut_scope(
    sut_id: str,
    body: SutScopeUpdate,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SutOut:
    """U7b: 更新该 Agent 的 RSI 可优化范围 → 200。

    ``optimization_scope=null/[]`` 恢复自动模式（平台扫描后自行决定）。
    非法项（不落在全局白名单 8 域内）→ 422 列出非法项。该范围仅约束
    RSI 自动优化（Critic/applier 强制收窄）；人工文件编辑仍只受全局
    安全边界约束。
    """
    sut = _get(store, sut_id)
    sut["optimization_scope"] = _validate_scope(sut_id, body.optimization_scope)
    from rsi.api.object_store import persist_record

    persist_record(request.app.state.services, "suts", sut)
    return SutOut(**sut)


@router.delete("/{sut_id}", status_code=204)
@audit.audited("sut.delete", object_type="sut")
async def delete_sut(
    sut_id: str,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    _get(store, sut_id)
    del store[sut_id]
    from rsi.api.object_store import delete_record

    delete_record(request.app.state.services, "suts", sut_id)
    return Response(status_code=204)


def _page_json(page: dict) -> str:
    return json.dumps(page)


# ── versioning + files (interfaces §9.2, 人工模式核心入口) ──────────────

_target_registry = TargetRegistry()
_ALL_TARGETS = set(TARGET_PATHS)


def _git(request: Request) -> Any:
    """Resolve the GitStore service (bootstrap always registers it)."""
    git = request.app.state.services.get("git_store")
    if git is None:
        raise NotFoundError("git store not configured", context={})
    return git


def _validate_editable(sut_id: str, path: str) -> None:
    """人工修改同在白名单执行层校验内（D-12 安全缝隙修复）。

    越界路径（冻结域 / 非优化目标域）→ 409 CONFLICT（§9.2 行内错误约定）。
    """
    rel = f"suts/{sut_id}/{path}"
    try:
        _target_registry.validate(rel, _ALL_TARGETS)
    except Exception as exc:
        raise ConflictError(
            f"path {path!r} outside the editable target domain",
            context={"sut_id": sut_id, "path": path,
                     "reason": getattr(exc, "context", {}).get("reason", "invalid")},
        ) from exc


class PromoteBody(BaseModel):
    candidate_version_id: str = Field(json_schema_extra={"example": "ab12cd34..."})
    baseline_batch_id: str | None = None
    candidate_batch_id: str | None = None
    reason: str | None = None


class RollbackBody(BaseModel):
    version_id: str


class SnapshotOut(BaseModel):
    version_id: str


class FileWriteBody(BaseModel):
    content: str
    commit: bool = False
    message: str = ""


class FileCreateBody(FileWriteBody):
    path: str


class FileContentOut(BaseModel):
    path: str
    content: str


class FileTreeOut(BaseModel):
    files: dict[str, str]


@router.post("/{sut_id}/versions:snapshot", response_model=SnapshotOut, status_code=201)
@audit.audited("version.snapshot", object_type="sut")
async def snapshot_sut(
    sut_id: str,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> SnapshotOut:
    """手动快照（FR6.5 同机制）→ 201{version_id}。"""
    _get(store, sut_id)
    try:
        vid = str(_git(request).snapshot(sut_id))
    except Exception as exc:
        raise NotFoundError(
            f"snapshot failed for sut {sut_id!r}: {exc}", context={"sut_id": sut_id}
        ) from exc
    return SnapshotOut(version_id=vid)


@router.post("/{sut_id}/versions:promote", status_code=204)
@audit.audited("version.promote", object_type="sut")
async def promote_sut(
    sut_id: str,
    body: PromoteBody,
    request: Request,
    user: Claims = Depends(require_perms("version:promote_rollback")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    """版本晋升 → 204。非 fast-forward → 409（NotFastForwardError 映射）。

    人工模式带双批引用与理由时自动登记 experiments（ADR-21；文档未定义
    实验记录字段集，实现取最小集：双批 id + reason + decision=promote）。
    """
    _get(store, sut_id)
    try:
        _git(request).promote(body.candidate_version_id)
    except Exception as exc:  # noqa: BLE001 - GitStoreError 无专用映射
        from rsi.core.errors import ConflictError

        msg = str(exc)
        if "no candidate branch" in msg:
            # F6/B2：手动 snapshot 是 baseline（无 cand/* 分支）——语义上
            # 无需 promote；报 409 而非 INTERNAL 500（S11 实测）。
            raise ConflictError(
                "该版本是手动基线（baseline），无需 promote；promote 仅用于"
                "优化器产出的候选版本（suts/<sut>/cand/* 分支）",
                context={"candidate_version_id": body.candidate_version_id},
            ) from exc
        raise
    if body.baseline_batch_id and body.candidate_batch_id:
        experiments = request.app.state.stores["experiments"]
        exp = {
            "id": f"exp-{uuid.uuid4().hex[:8]}",
            "sut_id": sut_id,
            "iteration_id": None,
            "versions": {
                "a": {"batch_id": body.baseline_batch_id},
                "b": {
                    "batch_id": body.candidate_batch_id,
                    "version_id": body.candidate_version_id,
                },
            },
            "decision": "promote",
            "decision_reason": body.reason or "",
            "evidence": [],
            "stale": False,
        }
        experiments[exp["id"]] = exp
    return Response(status_code=204)


@router.post("/{sut_id}/versions:rollback", status_code=204)
@audit.audited("version.rollback", object_type="sut")
async def rollback_sut(
    sut_id: str,
    body: RollbackBody,
    request: Request,
    user: Claims = Depends(require_perms("version:promote_rollback")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    """版本回滚 → 204；版本不存在 → 404。"""
    _get(store, sut_id)
    try:
        _git(request).rollback(sut_id, body.version_id)
    except Exception as exc:
        raise NotFoundError(
            f"rollback failed for sut {sut_id!r}: {exc}", context={"sut_id": sut_id}
        ) from exc
    return Response(status_code=204)


@router.get("/{sut_id}/lineage")
async def sut_lineage(
    sut_id: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("suts")),
) -> dict:
    """版本谱系 → {nodes:[{version_id,label,kind,parent_ids,experiment_ids}], edges}。"""
    _get(store, sut_id)
    try:
        graph = _git(request).lineage(sut_id)
    except Exception as exc:
        raise NotFoundError(
            f"lineage failed for sut {sut_id!r}: {exc}", context={"sut_id": sut_id}
        ) from exc
    return {
        "nodes": [
            {
                "version_id": n.version_id,
                "label": n.label,
                "kind": n.kind,
                "parent_ids": n.parent_ids,
                "experiment_ids": n.experiment_ids,
            }
            for n in graph.nodes
        ],
        "edges": [list(e) for e in graph.edges],
    }


@router.get("/{sut_id}/verify-image")
async def verify_image(
    sut_id: str,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> JSONResponse:
    """镜像验证（G5 / DD-05 §4）。

    B-5: routed to HostService.verify_image when a host service is present in
    the DI container; without one (bare test apps) the 501 placeholder stays.
    """
    sut = _get(store, sut_id)
    host_service = request.app.state.services.get("host_service")
    if host_service is None or not hasattr(host_service, "verify_image"):
        return JSONResponse(
            status_code=501,
            content={
                "code": "NOT_IMPLEMENTED",
                "message": "verify-image 待 S11 载体镜像检查（docker inspect + run --version）实现",
                "detail": {"sut_id": sut_id},
            },
        )
    image_ref = str(sut.get("image_ref") or sut.get("image") or sut_id)
    check = await host_service.verify_image(image_ref)
    return JSONResponse(
        status_code=200,
        content={
            "ok": check.ok,
            "image_ref": check.image_ref,
            "checks": check.checks,
            "detail": check.detail,
        },
    )


@router.get("/{sut_id}/files", response_model=FileTreeOut)
async def list_sut_files(
    sut_id: str,
    request: Request,
    path: str = "",
    version: str | None = None,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("suts")),
) -> FileTreeOut:
    """浏览 staging（version 省略）或历史版本的 SUT 文件（P0 人工模式入口）。"""
    _get(store, sut_id)
    try:
        files = _git(request).list_files(sut_id, version)
    except Exception as exc:
        raise NotFoundError(
            f"list_files failed for sut {sut_id!r}: {exc}",
            context={"sut_id": sut_id, "version": version},
        ) from exc
    # files 的 key 是相对 staging root 的全路径；按 SUT 前缀裁剪
    prefix = f"suts/{sut_id}/"
    trimmed = {k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)}
    if path:
        if path in trimmed:
            return FileTreeOut(files={path: trimmed[path]})
        sub = f"{path}/"
        trimmed = {k: v for k, v in trimmed.items() if k.startswith(sub)}
    return FileTreeOut(files=trimmed)


def _write_file(
    request: Request, sut_id: str, path: str, body: FileWriteBody
) -> None:
    _validate_editable(sut_id, path)
    _git(request).write_file(sut_id, path, body.content, body.commit, body.message)


@router.put("/{sut_id}/files/{path:path}", status_code=204)
@audit.audited("sut.file_write", object_type="sut")
async def write_sut_file(
    sut_id: str,
    path: str,
    body: FileWriteBody,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    """写 staging worktree（commit=false）或经 TargetRegistry 校验后提交新版本
    （commit=true）。越界路径 → 409（D-12：人工修改同在白名单校验内）。"""
    _get(store, sut_id)
    _write_file(request, sut_id, path, body)
    return Response(status_code=204)


@router.post("/{sut_id}/files", status_code=201)
@audit.audited("sut.file_create", object_type="sut")
async def create_sut_file(
    sut_id: str,
    body: FileCreateBody,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    """新建/上传文件（staging）→ 201。"""
    _get(store, sut_id)
    _write_file(request, sut_id, body.path, body)
    return Response(status_code=201)


@router.delete("/{sut_id}/files/{path:path}", status_code=204)
@audit.audited("sut.file_delete", object_type="sut")
async def delete_sut_file(
    sut_id: str,
    path: str,
    request: Request,
    user: Claims = Depends(require_perms("sut:manage")),
    store: dict = Depends(get_store("suts")),
) -> Response:
    """删除 staging 文件 → 204；越界路径 → 409。"""
    _get(store, sut_id)
    _validate_editable(sut_id, path)
    _git(request).delete_file(sut_id, path)
    return Response(status_code=204)
