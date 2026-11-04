"""rsi.core.naming: identifier generation and validation (DD-01 §4.1).

Entity IDs follow ``<prefix>-<YYYYMMDDHHmmss UTC>-<8-char random>`` where the
random segment uses a lowercase Crockford base32 alphabet (excludes ``i l o u``)
driven by :mod:`secrets`.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

from rsi.core.errors import NamingError

PREFIXES: frozenset[str] = frozenset(
    {"batch", "run", "lab", "snap", "iter", "jd", "hyp", "exp", "inst"}
)

# Crockford base32, lowercased, excluding i l o u.
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"

_ID_RE = re.compile(
    r"^(?P<prefix>[a-z]+)-(?P<ts>\d{14})-(?P<rand>[0-9a-hjkmnp-tv-z]{8})$"
)
_SUT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,31}$")
_EVALSET_RE = re.compile(r"^[a-z][a-z0-9]*-(holdout|iterate|smoke|general)$")
_TASK_RE = re.compile(r"^(fault|inspection|cutover)/[a-z][a-z0-9-]*-\d{2}$")


def _random_segment() -> str:
    # 40 bits -> exactly 8 Crockford base32 characters (5 bits each).
    value = int.from_bytes(secrets.token_bytes(5), "big")
    chars = []
    for shift in range(35, -1, -5):
        chars.append(_CROCKFORD[(value >> shift) & 0x1F])
    return "".join(chars)


def new_id(prefix: str) -> str:
    """Generate ``<prefix>-<YYYYMMDDHHmmss UTC>-<8-char Crockford base32>``."""
    if prefix not in PREFIXES:
        raise NamingError(
            f"invalid id prefix {prefix!r}", prefix=prefix, allowed=sorted(PREFIXES)
        )
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{ts}-{_random_segment()}"


def validate_id(prefix: str, value: str) -> None:
    """Validate an entity ID against the canonical format; raise NamingError."""
    if prefix not in PREFIXES:
        raise NamingError(
            f"invalid id prefix {prefix!r}", prefix=prefix, allowed=sorted(PREFIXES)
        )
    m = _ID_RE.match(value)
    if not m or m.group("prefix") != prefix:
        raise NamingError(
            f"malformed id {value!r} for prefix {prefix!r}", value=value, prefix=prefix
        )
    ts = m.group("ts")
    try:
        datetime.strptime(ts, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise NamingError(
            f"invalid timestamp segment in id {value!r}", value=value
        ) from exc


def validate_sut_id(v: str) -> None:
    """SUT id: ``^[a-z][a-z0-9-]{2,31}$``."""
    if not _SUT_ID_RE.match(v):
        raise NamingError(f"invalid sut_id {v!r}", value=v)


def validate_evalset_id(v: str) -> None:
    """EvalSet id: ``<domain>-<holdout|iterate|smoke|general>``."""
    if not _EVALSET_RE.match(v):
        raise NamingError(
            f"invalid evalset_id {v!r}", value=v,
            allowed_suffixes=["holdout", "iterate", "smoke", "general"],
        )


def validate_task_id(v: str) -> None:
    """Task id: ``<fault|inspection|cutover>/<phenomenon>-<two-digit seq>``."""
    if not _TASK_RE.match(v):
        raise NamingError(
            f"invalid task_id {v!r}", value=v,
            allowed_categories=["fault", "inspection", "cutover"],
        )


def git_refs(sut_id: str, n: int, *, hyp_id: str | None = None) -> tuple[str, str]:
    """Return ``(baseline_tag, candidate_branch)`` for a SUT.

    Baseline: ``suts/<sut_id>/baseline/vN``; candidate: ``suts/<sut_id>/cand/<hyp_id>``
    (or ``.../cand/pending`` when the hypothesis id is not yet known).
    """
    validate_sut_id(sut_id)
    if n < 1:
        raise NamingError(f"baseline number must be >= 1, got {n}", n=n)
    if hyp_id is not None:
        validate_id("hyp", hyp_id)
        cand = f"suts/{sut_id}/cand/{hyp_id}"
    else:
        cand = f"suts/{sut_id}/cand/pending"
    return f"suts/{sut_id}/baseline/v{n}", cand


__all__ = [
    "PREFIXES",
    "new_id",
    "validate_id",
    "validate_sut_id",
    "validate_evalset_id",
    "validate_task_id",
    "git_refs",
]
