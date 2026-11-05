"""Optimization-profile endpoints (DD-13 §9.5). P0: in-memory store."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from rsi.api.deps import audit, get_current_user, get_store, require_perms
from rsi.api.pagination import DEFAULT_LIMIT, paginate
from rsi.core.errors import NotFoundError
from rsi.core.models import OptimizationProfile
from rsi.security.auth import Claims

router = APIRouter(prefix="/profiles", tags=["profiles"])


class ProfileOut(BaseModel):
    id: str
    profile: OptimizationProfile
    created_at: str


class ProfilePage(BaseModel):
    items: list[ProfileOut]
    next_cursor: str | None = None
    has_more: bool = False


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, json_schema_extra={"example": "bgp-triage-tuning"})
    evalsets: dict | None = Field(
        default=None,
        json_schema_extra={"example": {"iterate": "es-abc12345", "holdout": "es-def67890", "smoke": "es-aaa11111"}},
    )
    goals: list | None = Field(
        default=None,
        json_schema_extra={"example": [{"metric": "task_success_rate", "direction": "max", "weight": 1.0}]},
    )
    enabled_targets: set[str] | None = Field(
        default=None,
        json_schema_extra={"example": ["system_prompt", "skills", "memory"]},
    )
    guards: dict | None = Field(
        default=None,
        json_schema_extra={"example": {"missed_check_worsen": "reject", "safety_any_worsen": "reject", "high_risk_worsen": "reject"}},
    )
    stop: dict | None = Field(
        default=None, json_schema_extra={"example": {"max_rounds": 20, "no_gain_rounds": 5}}
    )
    budget: dict | None = Field(
        default=None,
        json_schema_extra={"example": {"max_tokens": 1000000, "max_wall_hours": 8.0, "max_cost_usd": 50.0}},
    )
    llm_compare: dict | None = Field(
        default=None, json_schema_extra={"example": {"candidates": ["glm-4.7", "glm-4.6"]}}
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get(store: dict, profile_id: str) -> dict:
    try:
        return store[profile_id]
    except KeyError:
        raise NotFoundError(
            f"profile {profile_id!r} not found", context={"profile_id": profile_id}
        ) from None


@router.get("", response_model=ProfilePage)
async def list_profiles(
    response: Response,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("profiles")),
) -> ProfilePage:
    items, page = paginate(list(store.values()), cursor, limit)
    response.headers["x-pagination"] = json.dumps(page)
    return ProfilePage(
        items=[ProfileOut(**i) for i in items],
        next_cursor=page['next_cursor'],
        has_more=page['has_more'],
    )


@router.post("", response_model=ProfileOut, status_code=201)
@audit.audited("profile.create", object_type="profile")
async def create_profile(
    body: OptimizationProfile,
    user: Claims = Depends(require_perms("profile:manage")),
    store: dict = Depends(get_store("profiles")),
) -> ProfileOut:
    record = {
        "id": f"prof-{uuid.uuid4().hex[:8]}",
        "profile": body,
        "created_at": _now(),
    }
    store[record["id"]] = record
    return ProfileOut(**record)


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(
    profile_id: str,
    user: Claims = Depends(get_current_user),
    store: dict = Depends(get_store("profiles")),
) -> ProfileOut:
    return ProfileOut(**_get(store, profile_id))


@router.put("/{profile_id}", response_model=ProfileOut)
@audit.audited("profile.update", object_type="profile")
async def update_profile(
    profile_id: str,
    body: ProfilePatch,
    user: Claims = Depends(require_perms("profile:manage")),
    store: dict = Depends(get_store("profiles")),
) -> ProfileOut:
    record = _get(store, profile_id)
    updates = body.model_dump(exclude_unset=True)
    if updates:
        merged = record["profile"].model_dump()
        merged.update(updates)
        record["profile"] = OptimizationProfile.model_validate(merged)
    return ProfileOut(**record)


@router.delete("/{profile_id}", status_code=204)
@audit.audited("profile.delete", object_type="profile")
async def delete_profile(
    profile_id: str,
    user: Claims = Depends(require_perms("profile:manage")),
    store: dict = Depends(get_store("profiles")),
) -> Response:
    _get(store, profile_id)
    del store[profile_id]
    return Response(status_code=204)
