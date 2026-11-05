"""TargetRegistry: optimization path domain (DD-11 §3, FR6.1).

Declarative table of the 8 optimization targets (suts/<id>/ path regexes)
plus the frozen domains (judge/prompts/evalsets) that no changeset may ever
touch. ``validate`` is the executor-layer check invoked by ChangeApplier and
the DeterministicCritic (double validation, DD-11 §4).
"""

from __future__ import annotations

import re

from rsi.core.errors import TargetViolationError

__all__ = ["FROZEN_PATHS", "TARGET_PATHS", "TargetRegistry"]

# 优化项 -> suts/<id>/ path regex; {id} matches one path segment.
TARGET_PATHS: dict[str, str] = {
    "system_prompt": r"suts/{id}/system-prompt(\.md)?",
    "skills": r"suts/{id}/skills/.*",
    "scripts": r"suts/{id}/scripts/.*",
    "mcp_config": r"suts/{id}/mcp\.json",
    "mcp_src": r"suts/{id}/mcp-src/.*",
    "memory": r"suts/{id}/memory/.*",
    "knowledge": r"suts/{id}/knowledge/.*",
    "model_config": r"suts/{id}/model\.json",
}

FROZEN_PATHS: list[str] = [r"judge/.*", r"prompts/.*", r"evalsets/.*"]

_ID = r"[^/]+"


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile("^" + pattern.replace("{id}", _ID) + "$")


class TargetRegistry:
    """Whitelist / frozen-domain boundary for SUT changesets."""

    TARGET_PATHS: dict[str, str] = dict(TARGET_PATHS)
    FROZEN_PATHS: tuple[str, ...] = tuple(FROZEN_PATHS)

    def __init__(self) -> None:
        self._target_re = {
            name: _compile(p) for name, p in self.TARGET_PATHS.items()
        }
        self._frozen_re = [_compile(p) for p in self.FROZEN_PATHS]

    def match_target(self, path: str) -> str | None:
        """Return the target a path belongs to, or ``None`` when out of domain."""
        for name, rx in self._target_re.items():
            if rx.match(path):
                return name
        return None

    def is_frozen(self, path: str) -> bool:
        return any(rx.match(path) for rx in self._frozen_re)

    def validate(self, path: str, enabled_targets: set[str]) -> None:
        """Raise ``TargetViolationError`` unless *path* is an enabled, unfrozen target."""
        if self.is_frozen(path):
            raise TargetViolationError(
                f"path {path!r} is in a frozen domain",
                context={"path": path, "reason": "frozen_domain"},
            )
        target = self.match_target(path)
        if target is None:
            raise TargetViolationError(
                f"path {path!r} matches no optimization target",
                context={"path": path, "reason": "path_out_of_domain"},
            )
        if target not in enabled_targets:
            raise TargetViolationError(
                f"path {path!r} belongs to target {target!r} which is not enabled",
                context={
                    "path": path,
                    "reason": "target_not_enabled",
                    "target": target,
                },
            )
