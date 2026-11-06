"""Credential endpoints (interfaces §9.19).

值永不回显（脱敏响应——绝无 secret 字段）。secret 的存储委托
``credential_store`` 服务（加密）；服务不可用时退化为内存暂存（P0 测试用），
secret 仍永不回显。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/credentials", tags=["credentials"])

Kind = Literal["device_ssh", "nms_api", "llm_api", "backend_ssh"]


class CredentialCreate(BaseModel):
    name: str = Field(json_schema_extra={"example": "core-sw-ssh"})
    kind: Kind = Field(json_schema_extra={"example": "device_ssh"})
    secret: str = Field(json_schema_extra={"example": "----- PRIVATE -----"})

    model_config = {"json_schema_extra": {"secret_writeonly": True}}


class CredentialRotate(BaseModel):
    secret: str


class CredentialOut(BaseModel):
    """响应模型刻意不含 secret（§9.19 值永不回显）。"""

    id: str
    name: str
    kind: str
    created_at: str


class CredentialPage(BaseModel):
    items: list[CredentialOut]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, cred_id: str) -> dict:
    try:
        return store[cred_id]
    except KeyError:
        raise NotFoundError(
            f"credential {cred_id!r} not found", context={"credential_id": cred_id}
        ) from None


def _put_secret(request: Request, name: str, kind: str, secret: str) -> str:
    cs = request.app.state.services.get("credential_store")
    if cs is not None:
        return str(cs.put(name, kind, secret))
    # 文档未定义无 credential_store 服务的 P0 行为，实现取内存暂存 id。
    return f"mem-{uuid.uuid4().hex[:8]}"


def _rotate_secret(request: Request, cred_id: str, new_secret: str) -> None:
    cs = request.app.state.services.get("credential_store")
    if cs is not None:
        cs.rotate(cred_id, new_secret)


@router.get("", response_model=CredentialPage)
async def list_credentials(
    user: Claims = Depends(require_perms("credential:manage")),
    store: dict = Depends(get_store("credentials")),
) -> CredentialPage:
    return CredentialPage(items=[CredentialOut(**c) for c in store.values()])


@router.post("", response_model=CredentialOut, status_code=201)
@audit.audited("credential.create", object_type="credential")
async def create_credential(
    body: CredentialCreate,
    request: Request,
    user: Claims = Depends(require_perms("credential:manage")),
    store: dict = Depends(get_store("credentials")),
) -> CredentialOut:
    ref = _put_secret(request, body.name, body.kind, body.secret)
    cred = {
        "id": ref,
        "name": body.name,
        "kind": body.kind,
        "created_at": _now(),
    }
    store[cred["id"]] = cred
    return CredentialOut(**cred)


@router.post("/{cred_id}:rotate", status_code=204)
@audit.audited("credential.rotate", object_type="credential")
async def rotate_credential(
    cred_id: str,
    body: CredentialRotate,
    request: Request,
    user: Claims = Depends(require_perms("credential:manage")),
    store: dict = Depends(get_store("credentials")),
) -> Response:
    _get(store, cred_id)
    _rotate_secret(request, cred_id, body.secret)
    return Response(status_code=204)
