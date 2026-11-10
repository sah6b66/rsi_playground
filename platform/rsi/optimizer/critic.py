"""DeterministicCritic — DD-11 §5.2 pre-execution deterministic checks.

Five cheap, fully deterministic checks run on every changeset before any
expensive LLM critic or lab evaluation is spent. Returns a list of failure
reasons; an empty list means the changeset passed all checks.
"""

from __future__ import annotations

from typing import Any

from rsi.log import get_logger
from rsi.optimizer.targets import TargetRegistry, matches_scope

__all__ = ["DeterministicCritic"]

logger = get_logger(__name__)

PATH_OUT_OF_DOMAIN = "path_out_of_domain"
FROZEN_DOMAIN = "frozen_domain"
TARGET_NOT_ENABLED = "target_not_enabled"
DECLARATION_MISMATCH = "declaration_mismatch"
MULTI_TARGET = "multi_target"
OUT_OF_SCOPE = "out_of_scope"


class DeterministicCritic:
    """5 deterministic checks over a changeset's file list (DD-11 §5.2)."""

    def __init__(self, registry: TargetRegistry | None = None) -> None:
        self.registry = registry or TargetRegistry()

    def check(
        self,
        changeset_files: list[dict[str, Any]],
        enabled_targets: set[str],
        allowed_paths: list[str] | None = None,
    ) -> list[str]:
        """Return failure reasons (empty list = pass).

        Each *changeset_files* entry is a :class:`FileChange`-shaped dict
        with at least ``path``; ``target`` / ``declared`` are optional
        declaration metadata checked for consistency.

        *allowed_paths* (U7b, per-agent optimization scope) narrows the
        global whitelist further: items are relative to the SUT config-tree
        root (``skills/a/`` directory prefix, otherwise fnmatch). ``None``
        means no per-agent restriction (automatic mode). This constrains
        RSI auto-optimization only — manual FileEditor edits are bounded by
        the global whitelist alone.
        """
        failures: list[str] = []
        targets_seen: set[str] = set()

        for fc in changeset_files:
            path = str(fc.get("path", ""))
            if not path:
                failures.append("path_out_of_domain: (empty path)")
                continue

            if self.registry.is_frozen(path):
                failures.append(f"frozen_domain: {path}")
                continue

            target = self.registry.match_target(path)
            if target is None:
                failures.append(f"path_out_of_domain: {path}")
                continue
            targets_seen.add(target)

            if target not in enabled_targets:
                failures.append(
                    f"target_not_enabled: {path} -> {target} "
                    f"(enabled: {sorted(enabled_targets)})"
                )
                continue

            if allowed_paths is not None and not matches_scope(path, allowed_paths):
                failures.append(
                    f"out_of_scope: {path} 超出该 Agent 的优化范围 "
                    f"(scope: {allowed_paths})"
                )
                continue

            declared = fc.get("target")
            declared_flag = fc.get("declared")
            if (declared is not None and declared != target) or (
                declared_flag is False
            ):
                failures.append(
                    f"declaration_mismatch: {path} declared "
                    f"{declared!r} but belongs to {target!r}"
                )

        if len(targets_seen) > 1:
            failures.append(
                f"multi_target: changeset spans {sorted(targets_seen)}"
            )

        logger.debug(
            "check.exit",
            files=len(changeset_files),
            failures=len(failures),
        )
        return failures
