"""Hypothesis endpoints (interfaces §9.10).

P1 人工导入通道：draft 创建 → 修订 → submit-review（进入 Critic）。
P0: in-memory store keyed by hypothesis id.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import ConflictError, NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/hypotheses", tags=["hypotheses"])

# draft -> submitted -> withdrawn (§9.10: withdrawn 状态流转；文档未定义
# 完整状态机，实现取默认: draft 提交后为 submitted，可 withdrawn)
_VALID_STATUS = {"draft", "submitted", "withdrawn"}


class HypothesisCreate(BaseModel):
    """文档未定义 Hypothesis 字段集，实现取 P1 人工导入所需最小集。"""

    model_config = {"extra": "allow"}

    sut_id: str = Field(json_schema_extra={"example": "sut-1a2b3c4d"})
    iteration_id: str | None = Field(default=None)
    statement: str = Field(
        json_schema_extra={
            "example": "Adding explicit verification steps reduces missed checks"
        }
    )
    target: str | None = Field(default=None)
    predicted_effect: str | None = Field(default=None)
    status: str = Field(default="draft")


class HypothesisUpdate(BaseModel):
    model_config = {"extra": "allow"}

    statement: str | None = Field(default=None)
    target: str | None = Field(default=None)
    predicted_effect: str | None = Field(default=None)
    status: str | None = Field(default=None)


class HypothesisOut(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    sut_id: str
    iteration_id: str | None
    statement: str
    target: str | None
    predicted_effect: str | None
    status: str
    created_at: str
    updated_at: str


class HypothesisPage(BaseModel):
    items: list[HypothesisOut]
    next_cursor: str | None = None
    has_more: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, hyp_id: str) -> dict:
    try:
        return store[hyp_id]
    except KeyError:
        raise NotFoundError(
            f"hypothesis {hyp_id!r} not found", context={"hypothesis_id": hyp_id}
        ) from None


@router.get("", response_model=HypothesisPage)
async def list_hypotheses(
    response: Response,
    sut_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("hypotheses")),
) -> HypothesisPage:
    items = list(store.values())
    if sut_id is not None:
        items = [h for h in items if h["sut_id"] == sut_id]
    if status is not None:
        items = [h for h in items if h["status"] == status]
    page_items, page = paginate(items, cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return HypothesisPage(
        items=[HypothesisOut(**h) for h in page_items],
        next_cursor=page["next_cursor"],
        has_more=page["has_more"],
    )


@router.post("", response_model=HypothesisOut, status_code=201)
@audit.audited("hypothesis.create", object_type="hypothesis")
async def create_hypothesis(
    body: HypothesisCreate,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("hypotheses")),
) -> HypothesisOut:
    now = _now()
    hyp: dict[str, Any] = {
        "id": f"hyp-{uuid.uuid4().hex[:8]}",
        "sut_id": body.sut_id,
        "iteration_id": body.iteration_id,
        "statement": body.statement,
        "target": body.target,
        "predicted_effect": body.predicted_effect,
        "status": "draft",
        "created_at": now,
        "updated_at": now,
    }
    store[hyp["id"]] = hyp
    return HypothesisOut(**hyp)


@router.get("/{hyp_id}", response_model=HypothesisOut)
async def get_hypothesis(
    hyp_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("hypotheses")),
) -> HypothesisOut:
    return HypothesisOut(**_get(store, hyp_id))


@router.put("/{hyp_id}", response_model=HypothesisOut)
@audit.audited("hypothesis.update", object_type="hypothesis")
async def update_hypothesis(
    hyp_id: str,
    body: HypothesisUpdate,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("hypotheses")),
) -> HypothesisOut:
    hyp = _get(store, hyp_id)
    updates = body.model_dump(exclude_unset=True)
    new_status = updates.pop("status", None)
    if new_status is not None:
        if new_status not in _VALID_STATUS:
            raise ConflictError(
                f"invalid hypothesis status {new_status!r}",
                context={"hypothesis_id": hyp_id, "status": new_status},
            )
        if hyp["status"] == "withdrawn":
            raise ConflictError(
                "withdrawn hypothesis cannot be modified",
                context={"hypothesis_id": hyp_id, "status": hyp["status"]},
            )
        hyp["status"] = new_status
    hyp.update({k: v for k, v in updates.items() if v is not None})
    hyp["updated_at"] = _now()
    return HypothesisOut(**hyp)


@router.post("/{hyp_id}:submit-review", status_code=204)
@audit.audited("hypothesis.submit_review", object_type="hypothesis")
async def submit_review(
    hyp_id: str,
    user: Claims = Depends(require_perms("evalset:manage")),
    store: dict = Depends(get_store("hypotheses")),
) -> Response:
    hyp = _get(store, hyp_id)
    if hyp["status"] != "draft":
        raise ConflictError(
            f"hypothesis in state {hyp['status']!r} cannot enter review",
            context={"hypothesis_id": hyp_id, "status": hyp["status"]},
        )
    hyp["status"] = "submitted"
    hyp["updated_at"] = _now()
    return Response(status_code=204)
