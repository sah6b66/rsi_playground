"""ChangeApplier — DD-11 §4 apply a hypothesis as a candidate version.

Pipeline: snapshot current SUT (GitStore in production, stub in P0 tests)
→ build a :class:`Changeset` from the hypothesis ``diff_content`` or
``model_config_patch`` → validate every path against the TargetRegistry
(enabled targets only, frozen domains never) → return the candidate version
id. Nothing here promotes anything; promotion is the decision layer's call.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from rsi.core.errors import TargetViolationError
from rsi.log import get_logger
from rsi.optimizer.targets import TargetRegistry, matches_scope
from rsi.versioning.changeset import Changeset, FileChange, VersionId

__all__ = ["ChangeApplier"]

logger = get_logger(__name__)

# Unified-diff header paths: "+++ b/suts/x/skills/y.md", "+++ suts/x/...", tab-safe.
_DIFF_PATH_RE = re.compile(r"^\+\+\+\s+(?:b/)?(\S+?)(?:\t\S*)?$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")


class ChangeApplier:
    """Apply a hypothesis changeset and return the candidate version id."""

    def __init__(self, registry: TargetRegistry | None = None, gitstore: Any = None) -> None:
        self.registry = registry or TargetRegistry()
        self.gitstore = gitstore  # optional GitStore (DD-09); None -> stub

    def apply(
        self,
        sut_id: str,
        hypothesis: dict[str, Any],
        enabled_targets: set[str],
        allowed_paths: list[str] | None = None,
    ) -> str:
        """1. Snapshot (stub / GitStore), 2. build changeset, 3. validate,
        4. return candidate version id (GitStore commit or deterministic id).

        *allowed_paths* (U7b, per-agent optimization scope) narrows the
        global whitelist further (items relative to the SUT tree root:
        ``dir/`` prefix, otherwise fnmatch); ``None`` = automatic mode.
        This constrains RSI auto-optimization only — manual FileEditor
        writes stay bounded by the global whitelist alone.
        """
        hyp_id = str(hypothesis.get("hypothesis_id") or hypothesis.get("id") or "hyp")

        # 1. Snapshot the current SUT so the candidate has an explicit base.
        if self.gitstore is not None:
            base_version = self.gitstore.snapshot(sut_id)
        else:
            base_version = VersionId("0" * 40)  # stub base (P0, no GitStore)

        # 2. Build the changeset from the hypothesis.
        files: list[FileChange]
        diff_content = hypothesis.get("diff_content")
        model_config_patch = hypothesis.get("model_config_patch")
        if diff_content is not None:
            files = self._files_from_diff(sut_id, str(diff_content))
        elif model_config_patch is not None:
            files = [
                FileChange(
                    path=f"suts/{sut_id}/model.json",
                    op="edit",
                    content=_render_json_patch(model_config_patch),
                    target="model_config",
                    declared=True,
                )
            ]
        else:
            raise TargetViolationError(
                "hypothesis carries neither diff_content nor model_config_patch",
                context={"reason": "empty_changeset", "hypothesis_id": hyp_id},
            )
        if not files:
            raise TargetViolationError(
                "diff_content contains no file paths",
                context={"reason": "empty_changeset", "hypothesis_id": hyp_id},
            )

        changeset = Changeset(hypothesis_id=hyp_id, base_version=base_version, files=files)

        # 3. Validate every path against the TargetRegistry (double
        #    validation: the DeterministicCritic runs the same registry).
        for fc in changeset.files:
            self.registry.validate(fc.path, enabled_targets)
            # U7b per-agent scope enforcement (belt-and-suspenders: the
            # DeterministicCritic runs the same check before apply).
            if allowed_paths is not None and not matches_scope(
                fc.path, allowed_paths
            ):
                raise TargetViolationError(
                    f"path {fc.path!r} 超出该 Agent 的优化范围",
                    context={
                        "path": fc.path,
                        "reason": "out_of_scope",
                        "allowed_paths": list(allowed_paths),
                    },
                )

        # 4. Candidate version id.
        if self.gitstore is not None:
            version = self.gitstore.commit_candidate(
                base_version, changeset, hyp_id
            )
        else:
            version = self._stub_version(changeset)
        logger.debug(
            "apply.exit", sut_id=sut_id, hypothesis_id=hyp_id,
            files=len(changeset.files), version=str(version),
        )
        return str(version)

    # -- internals ----------------------------------------------------------------

    def _files_from_diff(self, sut_id: str, diff: str) -> list[FileChange]:
        """Extract FileChanges from a unified diff.

        Paths are taken from ``diff --git`` or ``+++`` headers; a path that
        does not already live under ``suts/<sut_id>/`` is anchored there
        (the applier may only touch that SUT's tree).
        """
        raw_paths: list[str] = []
        for line in diff.splitlines():
            m = _DIFF_GIT_RE.match(line)
            if m:
                raw_paths.append(m.group(2))
                continue
            m = _DIFF_PATH_RE.match(line)
            if m and not m.group(1).startswith("a/"):
                raw_paths.append(m.group(1))

        files: list[FileChange] = []
        for path in raw_paths:
            if not path.startswith(f"suts/{sut_id}/"):
                path = f"suts/{sut_id}/{path}"
            target = self.registry.match_target(path)
            files.append(
                FileChange(
                    path=path,
                    op="edit",
                    content=diff,
                    target=target or "custom",
                    declared=target is not None,
                )
            )
        return files

    @staticmethod
    def _stub_version(changeset: Changeset) -> VersionId:
        """Deterministic pseudo-SHA for the no-GitStore path (P0 tests)."""
        h = hashlib.sha256(changeset.model_dump_json().encode("utf-8"))
        return VersionId(h.hexdigest()[:40])


def _render_json_patch(patch: dict[str, Any]) -> str:
    import json

    return json.dumps(patch, indent=2, sort_keys=True, ensure_ascii=False)
