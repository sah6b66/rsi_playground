"""Secret hygiene: startup self-check + log redaction filter (DD-03 §4).

The log filter is a leak-containment net (exact-match replacement of
fingerprinted patterns with "***"), not the primary redaction path.
"""

from __future__ import annotations

import base64
from typing import Any, Iterable

from rsi.log import get_logger
from rsi.security.credentials import SecretFingerprint

_MASK = "***"

_logger = get_logger(__name__)


class SecretHygiene:
    @staticmethod
    def startup_check(master_key: str | None, jwt_secret: str | None) -> None:
        """Exit with a CRITICAL message unless both secrets are >=32 bytes."""
        problems: list[str] = []
        if not master_key:
            problems.append("RSI_MASTER_KEY is not set")
        else:
            try:
                n = len(base64.b64decode(master_key, validate=True))
            except Exception:
                problems.append("RSI_MASTER_KEY is not valid base64")
            else:
                if n < 32:
                    problems.append(f"RSI_MASTER_KEY decodes to {n} bytes (<32)")
        if not jwt_secret:
            problems.append("RSI_JWT_SECRET is not set")
        elif len(jwt_secret.encode()) < 32:
            problems.append("RSI_JWT_SECRET is shorter than 32 bytes")
        if problems:
            # A-11③: route through structured logging (JSON console) instead
            # of a bare print — same CRITICAL severity + SystemExit(1).
            _logger.critical(
                "secret_hygiene_check_failed", problems="; ".join(problems)
            )
            raise SystemExit(1)

    @staticmethod
    def install_log_filter(
        fingerprints: Iterable[SecretFingerprint],
    ) -> Any:
        """Return a structlog processor masking fingerprinted patterns."""

        def redact(
            logger: Any, method: Any, event_dict: dict[str, Any]
        ) -> dict[str, Any]:
            def _scrub(value: Any) -> Any:
                if isinstance(value, dict):
                    return {k: _scrub(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [_scrub(v) for v in value]
                if isinstance(value, str):
                    masked = value
                    for fp in fingerprints:
                        for p in fp.patterns:
                            if p:
                                masked = masked.replace(p, _MASK)
                    return masked
                return value

            return {k: _scrub(v) for k, v in event_dict.items()}

        return redact


__all__ = ["SecretHygiene"]
