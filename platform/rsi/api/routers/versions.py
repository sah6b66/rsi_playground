"""Version diff endpoint (interfaces §9.3).

``GET /versions/{a}/diff/{b} → [{path, op, patch}]`` — file-level diff between
any two versions, delegated to the GitStore service.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from rsi.api.deps import get_current_user
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(prefix="/versions", tags=["versions"])


def _git(request: Request) -> Any:
    git = request.app.state.services.get("git_store")
    if git is None:
        raise NotFoundError("git store not configured", context={})
    return git


@router.get("/{a}/diff/{b}")
async def diff_versions(
    a: str,
    b: str,
    request: Request,
    user: Claims = Depends(get_current_user),
) -> list[dict]:
    """File-level diff between two commit SHAs (Monaco 渲染数据源)."""
    git = _git(request)
    try:
        diffs = git.diff(a, b)
    except Exception as exc:  # GitStoreError and friends -> 404 per §9.0
        raise NotFoundError(
            f"diff failed for {a!r}..{b!r}: {exc}",
            context={"a": a, "b": b},
        ) from exc
    return [
        {"path": d.path, "op": d.op, "patch": d.patch} for d in diffs
    ]
