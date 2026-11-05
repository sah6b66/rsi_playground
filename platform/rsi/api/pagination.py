"""Cursor pagination helpers (DD-13 §5.2).

Cursor encodes a JSON payload (``{"offset": n}``) as base64. Response header
``x-pagination`` carries ``{"next_cursor": ..., "has_more": ...}``.
"""

from __future__ import annotations

import base64
import json
from typing import Any, TypeVar

T = TypeVar("T")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return max(0, int(payload["offset"]))
    except Exception:
        return 0


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def paginate(
    items: list[T], cursor: str | None, limit: int = DEFAULT_LIMIT
) -> tuple[list[T], dict[str, Any]]:
    """Slice ``items`` by cursor offset; return page + x-pagination header value."""
    limit = clamp_limit(limit)
    offset = decode_cursor(cursor)
    window = items[offset : offset + limit]
    has_more = offset + limit < len(items)
    header = {
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
        "has_more": has_more,
    }
    return window, header


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "paginate",
]
