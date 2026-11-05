"""SafetyScanner — three-tier command rule engine (DD-08 §4/§5 note ②).

Frozen-rule subset for this slice (TU-011 fatal table):

fatal:
  * ``reset saved-configuration`` anywhere in the command
  * ``delete`` targeting a configuration archive file (.cfg / .zip / vrpcfg /
    startup / config substrings) — destroys the boot config
  * a bare ``undo`` (full command) — indiscriminate rollback
  * entering config mode (``system-view`` / ``configure terminal``) while the
    instance permission is ``read-only``
  * any command not starting with a read-only whitelist prefix
    (display/show/ping/tracert/screen-length) — unregistered high-risk

major: ``reboot`` / ``reload`` (disruptive but not config-destroying).
minor: ``save`` / ``commit`` (state-mutating persistence).
"""

from __future__ import annotations

from typing import Literal

from rsi.core.models import SafetyVerdict
from rsi.log import get_logger

__all__ = ["SafetyScanner"]

logger = get_logger(__name__)

Permission = Literal["read-only", "read-write"]

_READ_ONLY_PREFIXES: tuple[str, ...] = (
    "display",
    "show",
    "ping",
    "tracert",
    "screen-length",
)

_CONFIG_DELETE_MARKERS: tuple[str, ...] = (
    ".cfg",
    ".zip",
    "vrpcfg",
    "startup",
    "config",
)

_CONFIG_MODE_COMMANDS: frozenset[str] = frozenset({"system-view", "configure terminal"})

_MAJOR_PATTERNS: tuple[str, ...] = ("reboot", "reload")
_MINOR_PATTERNS: tuple[str, ...] = ("save", "commit")


class SafetyScanner:
    def scan(self, commands: list[str], permission: Permission) -> SafetyVerdict:
        logger.debug("scan.entry", commands=len(commands), permission=permission)
        violations: list[str] = []
        fatal = False
        major = 0
        minor = 0

        for cmd in commands:
            text = cmd.strip()
            low = text.lower()
            if not low:
                continue
            if self._is_fatal(low, permission):
                violations.append(text)
                fatal = True
                continue
            if any(p in low for p in _MAJOR_PATTERNS):
                violations.append(text)
                major += 1
                continue
            if any(low == p or low.startswith(p + " ") for p in _MINOR_PATTERNS):
                violations.append(text)
                minor += 1

        logger.debug(
            "scan.exit", violations=len(violations), fatal=fatal, major=major, minor=minor
        )
        return SafetyVerdict(
            violations=violations,
            fatal=fatal,
            major=major,
            minor=minor,
        )

    def _is_fatal(self, low: str, permission: Permission) -> bool:
        if "reset saved-configuration" in low:
            return True  # destroys boot config
        if low.startswith("delete ") and any(m in low for m in _CONFIG_DELETE_MARKERS):
            return True  # delete of a configuration archive file
        if low == "undo":
            return True  # full rollback
        if permission == "read-only" and low in _CONFIG_MODE_COMMANDS:
            return True  # config mode under read-only permission
        if any(p in low for p in _MAJOR_PATTERNS) or any(
            low == p or low.startswith(p + " ") for p in _MINOR_PATTERNS
        ):
            return False  # classified below-fatal before the catch-all
        if permission == "read-write" and low in _CONFIG_MODE_COMMANDS:
            return False  # registered config-mode entry under read-write
        return not low.startswith(_READ_ONLY_PREFIXES)  # unregistered high-risk
