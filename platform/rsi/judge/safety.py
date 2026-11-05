"""SafetyScanner — three-tier command rule engine (DD-08 §4/§5 note ②).

Rules live in the frozen domain file ``rsi/judge/rules/safety_rules.yaml``
(C-6); when the file is missing the builtin fallback table below is used so
deployments without the rules tree keep working (a warning is logged).

Behavior (identical to the pre-yaml hardcoded table):

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

import re
from pathlib import Path
from typing import Any, Literal

import yaml

from rsi.core.models import SafetyVerdict
from rsi.log import get_logger

__all__ = ["SafetyScanner", "RULES_PATH"]

logger = get_logger(__name__)

Permission = Literal["read-only", "read-write"]

RULES_PATH = Path(__file__).parent / "rules" / "safety_rules.yaml"

# -- builtin fallback table (deployments without the yaml file) ----------------
_FALLBACK: dict[str, Any] = {
    "read_only_prefixes": ["display", "show", "ping", "tracert", "screen-length"],
    "config_delete_markers": [".cfg", ".zip", "vrpcfg", "startup", "config"],
    "config_mode_commands": ["system-view", "configure terminal"],
    "fatal": [
        {"id": "reset_saved_configuration", "regex": "reset saved-configuration",
         "description": "destroys the saved boot configuration"},
        {"id": "delete_config_archive",
         "regex": r"^delete .*(?:\.cfg|\.zip|vrpcfg|startup|config)",
         "description": "deletes a configuration archive / startup configuration file"},
        {"id": "bare_undo", "regex": "^undo$",
         "description": "indiscriminate full rollback of all configuration"},
    ],
    "major_patterns": ["reboot", "reload"],
    "minor_patterns": ["save", "commit"],
}


def load_rules(path: Path | None = None) -> tuple[dict[str, Any], bool]:
    """Load the safety rule table; returns (rules, loaded_from_file)."""
    target = path if path is not None else RULES_PATH
    if target.is_file():
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "read_only_prefixes" in data:
                return data, True
            logger.warning("safety_rules.invalid", path=str(target))
        except yaml.YAMLError as exc:
            logger.warning(
                "safety_rules.parse_error", path=str(target), exc_type=type(exc).__name__
            )
    else:
        logger.warning("safety_rules.missing_fallback_builtin", path=str(target))
    return _FALLBACK, False


class SafetyScanner:
    def __init__(self, rules_path: Path | None = None) -> None:
        rules, loaded = load_rules(rules_path)
        self.rules_from_file = loaded
        self._prefixes: tuple[str, ...] = tuple(rules["read_only_prefixes"])
        self._delete_markers: tuple[str, ...] = tuple(rules["config_delete_markers"])
        self._config_mode: frozenset[str] = frozenset(rules["config_mode_commands"])
        self._fatal: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
            (re.compile(r["regex"], re.IGNORECASE), str(r["id"]), str(r.get("description", "")))
            for r in rules.get("fatal", [])
        )
        self._major: tuple[str, ...] = tuple(rules.get("major_patterns", ()))
        self._minor: tuple[str, ...] = tuple(rules.get("minor_patterns", ()))

    def fatal_rule_descriptions(self) -> dict[str, str]:
        """id -> description map for the fatal table (DD-08 note ②)."""
        return {rid: desc for _rx, rid, desc in self._fatal}

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
            if any(p in low for p in self._major):
                violations.append(text)
                major += 1
                continue
            if any(low == p or low.startswith(p + " ") for p in self._minor):
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
        for rx, _rid, _desc in self._fatal:
            if rx.search(low):
                return True
        if low.startswith("delete ") and any(m in low for m in self._delete_markers):
            return True  # delete of a configuration archive file
        if permission == "read-only" and low in self._config_mode:
            return True  # config mode under read-only permission
        if any(p in low for p in self._major) or any(
            low == p or low.startswith(p + " ") for p in self._minor
        ):
            return False  # classified below-fatal before the catch-all
        if permission == "read-write" and low in self._config_mode:
            return False  # registered config-mode entry under read-write
        return not low.startswith(self._prefixes)  # unregistered high-risk
