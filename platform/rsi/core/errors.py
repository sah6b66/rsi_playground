"""rsi.core.errors: exception hierarchy (DD-01 §3.12).

RsiError carries a human message plus a free-form ``context`` dict so that
upper layers (API, CLI, audit log) can surface structured details without
parsing strings.
"""

from __future__ import annotations

from typing import Any


class RsiError(Exception):
    """Base class for all RSI platform errors.

    Attributes:
        context: structured key/value details (device, path, output, ...).
    """

    def __init__(self, message: str, *, context: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(message)
        merged: dict[str, Any] = dict(context) if context else {}
        merged.update(kw)
        self.context: dict[str, Any] = merged


class ConfigError(RsiError):
    """Configuration invalid or missing."""


class NamingError(RsiError):
    """Identifier malformed or violates naming rules (incl. site overrides)."""


class EnvError(RsiError):
    """Sandbox environment build/prepare/restore failure."""


class DeviceError(RsiError):
    """SSH or command failure on a device (context: output, device, cli)."""


class CarrierError(RsiError):
    """Fault-carrier injection or lifecycle failure."""


class TraceError(RsiError):
    """Telemetry/trace collection or parsing failure."""


class JudgeError(RsiError):
    """Judge pipeline failure. (indeterminate is a Verdict, not an error)."""


class LLMOutputInvalid(RsiError):
    """LLM output failed schema validation after retries exhausted (ADR-1/7)."""


class UpstreamError(RsiError):
    """External system (Docker daemon, etc.) call failed (API 502)."""


class GitStoreError(RsiError):
    """Git-backed artifact store failure."""


class FreezeViolationError(GitStoreError):
    """Write attempted against a frozen ref."""


class NotFastForwardError(GitStoreError):
    """Push rejected: non-fast-forward update."""


class TargetViolationError(RsiError):
    """Whitelist / frozen-domain boundary breach (context: path, target)."""


class BudgetExceeded(RsiError):
    """Token/time/cost budget exhausted."""


class AuthError(RsiError):
    """Authentication or authorization failure."""


class InvalidTransitionError(RsiError):
    """Illegal state-machine transition (context: from_state, to_state)."""


class UnknownDeviceError(RsiError):
    """Referenced device not present in the site inventory."""


class NotFoundError(RsiError):
    """Referenced resource does not exist (API 404)."""


class ConflictError(RsiError):
    """State conflict: duplicate resource or illegal concurrent update (API 409)."""


class ValidationError(RsiError):
    """Semantically invalid input beyond schema shape (API 422)."""


__all__ = [
    "RsiError",
    "ConfigError",
    "NamingError",
    "EnvError",
    "DeviceError",
    "CarrierError",
    "TraceError",
    "JudgeError",
    "LLMOutputInvalid",
    "UpstreamError",
    "GitStoreError",
    "FreezeViolationError",
    "NotFastForwardError",
    "TargetViolationError",
    "BudgetExceeded",
    "AuthError",
    "InvalidTransitionError",
    "UnknownDeviceError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
]
