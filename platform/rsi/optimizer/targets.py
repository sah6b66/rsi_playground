"""TargetRegistry: optimization path domain (DD-11 §3, FR6.1).

Declarative table of the 8 optimization targets (suts/<id>/ path regexes)
plus the frozen domains (judge/prompts/evalsets) that no changeset may ever
touch. ``validate`` is the executor-layer check invoked by ChangeApplier and
the DeterministicCritic (double validation, DD-11 §4).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

from rsi.core.errors import TargetViolationError
from rsi.log import get_logger

__all__ = [
    "FROZEN_PATHS",
    "TARGET_PATHS",
    "TargetRegistry",
    "matches_scope",
]

logger = get_logger(__name__)

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

# D-12: editable_elements() metadata (DD-11 §4 — hypothesis generation input).
_TARGET_DESCRIPTIONS: dict[str, str] = {
    "system_prompt": "SUT 系统提示词（根指引，A/B 归因对照原文）",
    "skills": "SUT 技能包（可复用操作技能文件）",
    "scripts": "SUT 辅助脚本（可执行工具脚本）",
    "mcp_config": "MCP 服务器配置（mcp.json）",
    "mcp_src": "MCP 服务器源码（mcp-src/ 自研工具）",
    "memory": "SUT 记忆文件（跨轮次经验积累）",
    "knowledge": "知识库文件（故障处置知识/手册）",
    "model_config": "模型调用配置（model.json：模型/温度等）",
}

_TARGET_EXAMPLES: dict[str, str] = {
    "system_prompt": "suts/{sut}/system-prompt.md",
    "skills": "suts/{sut}/skills/bgp-triage.md",
    "scripts": "suts/{sut}/scripts/check_bgp.py",
    "mcp_config": "suts/{sut}/mcp.json",
    "mcp_src": "suts/{sut}/mcp-src/tools/ping.py",
    "memory": "suts/{sut}/memory/lessons.md",
    "knowledge": "suts/{sut}/knowledge/vpn-runbook.md",
    "model_config": "suts/{sut}/model.json",
}

_ID = r"[^/]+"


def matches_scope(full_path: str, allowed_paths: list[str]) -> bool:
    """Per-agent optimization-scope match (U7b, DD-11 §3).

    *full_path* is a ``suts/<id>/...`` changeset path; *allowed_paths* items
    are relative to the SUT config-tree root (no ``suts/<id>/`` prefix).
    Matching semantics per item:

    * item ends with ``/`` → directory prefix match (``skills/a/`` allows
      ``skills/a/x.md`` but not ``skills/ab/y.md``);
    * otherwise → fnmatch glob against the relative path
      (``mcp.json`` exact, ``skills/*.md`` glob).

    An empty *allowed_paths* allows nothing (callers normalize the
    "undefined = automatic mode" case to ``None`` before calling).
    """
    parts = PurePosixPath(full_path.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] != "suts":
        return False
    rel = "/".join(parts[2:])
    for item in allowed_paths:
        item = str(item).strip()
        if not item:
            continue
        if item.endswith("/"):
            if rel.startswith(item):
                return True
        elif fnmatch.fnmatch(rel, item):
            return True
    return False


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

    def editable_elements(self, enabled: set[str] | None = None) -> list[dict]:
        """Whitelist of editable elements for hypothesis generation (D-12).

        One entry per optimization target: ``{target, path_glob,
        description, examples}``; ``path_glob`` mirrors the whitelist regex
        (``{id}`` widened to ``*``). Pass ``enabled`` to restrict to the
        profile's enabled targets.
        """
        enabled = enabled if enabled is not None else set(self.TARGET_PATHS)
        elements: list[dict] = []
        for target, pattern in self.TARGET_PATHS.items():
            if target not in enabled:
                continue
            glob = pattern.replace("{id}", "*")
            elements.append(
                {
                    "target": target,
                    "path_glob": glob,
                    "description": _TARGET_DESCRIPTIONS.get(target, target),
                    "examples": [_TARGET_EXAMPLES.get(target, glob)],
                }
            )
        return elements

    def is_frozen(self, path: str) -> bool:
        return any(rx.match(path) for rx in self._frozen_re)

    def validate(self, path: str, enabled_targets: set[str]) -> None:
        """Raise ``TargetViolationError`` unless *path* is an enabled, unfrozen target."""
        logger.debug("validate.entry", path=path, enabled_targets=sorted(enabled_targets))
        # Path-traversal guard (audit R1#4): any ".." segment, absolute path
        # or backslash escape is rejected before the whitelist regexes run —
        # the regex tails (".*") would otherwise happily accept "..".
        if (
            ".." in PurePosixPath(path.replace("\\", "/")).parts
            or path.startswith("/")
            or "\\" in path
        ):
            logger.debug("validate.violation", reason="path_traversal", path=path)
            raise TargetViolationError(
                f"path {path!r} contains a traversal segment and is rejected",
                context={"path": path, "reason": "path_traversal"},
            )
        if self.is_frozen(path):
            logger.debug("validate.violation", reason="frozen_domain", path=path)
            raise TargetViolationError(
                f"path {path!r} is in a frozen domain",
                context={"path": path, "reason": "frozen_domain"},
            )
        target = self.match_target(path)
        if target is None:
            logger.debug("validate.violation", reason="path_out_of_domain", path=path)
            raise TargetViolationError(
                f"path {path!r} matches no optimization target",
                context={"path": path, "reason": "path_out_of_domain"},
            )
        if target not in enabled_targets:
            logger.debug("validate.violation", reason="target_not_enabled", path=path, target=target)
            raise TargetViolationError(
                f"path {path!r} belongs to target {target!r} which is not enabled",
                context={
                    "path": path,
                    "reason": "target_not_enabled",
                    "target": target,
                },
            )
