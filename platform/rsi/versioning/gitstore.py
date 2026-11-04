"""GitStore facade: snapshot / commit_candidate / promote / rollback (DD-09 §4-5).

A single bare repository holds all objects. A persistent *staging* worktree
is the source of truth for manual edits; per-operation temp worktrees are
created from arbitrary commits and always cleaned up (try/finally).

Refs layout::

    suts/<sut_id>/baseline/v<N>    # tags; N only increments (keep-all lineage)
    suts/<sut_id>/cand/<hyp_id>    # branches pointing at candidate commits
"""

from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path

import git

from rsi.core.errors import GitStoreError, NotFastForwardError
from rsi.versioning.changeset import Changeset, FileChange, VersionId
from rsi.versioning.worktrees import Worktrees

_BASELINE_RE = re.compile(r"^suts/(?P<sut>[^/]+)/baseline/v(?P<n>\d+)$")
_SUT_PATH_RE = re.compile(r"^suts/(?P<sut>[^/]+)/")


def _baseline_prefix(sut_id: str) -> str:
    return f"suts/{sut_id}/baseline/"


class GitStore:
    """Git-backed artifact store for SUT versions (FR6.5/FR8)."""

    def __init__(self, repo_path: Path, worktree_root: Path) -> None:
        self.repo_path = Path(repo_path)
        self.worktree_root = Path(worktree_root)
        self.repo: git.Repo | None = None
        self.worktrees: Worktrees | None = None
        # Serialize all mutating ops (snapshot/commit/promote/rollback).
        # Sync methods -> threading.Lock instead of asyncio.Lock (DD-09 §4).
        self._write_lock = threading.RLock()

    # ── lifecycle ────────────────────────────────────────────────────

    def init(self) -> None:
        """Initialize the bare repo + worktrees + staging worktree."""
        with self._write_lock:
            if self.repo is not None:
                return
            try:
                if self.repo_path.exists() and (self.repo_path / "HEAD").exists():
                    repo = git.Repo(self.repo_path)
                else:
                    repo = git.Repo.init(self.repo_path, bare=True)
            except git.GitCommandError as exc:  # pragma: no cover
                raise GitStoreError(f"repo init failed: {exc}") from exc
            self.repo = repo
            self.worktrees = Worktrees(self.worktree_root, repo=repo)
            # Touch staging so it exists from the start.
            self.worktrees.staging()

    def _require(self) -> tuple[git.Repo, Worktrees, Path]:
        if self.repo is None or self.worktrees is None:
            raise GitStoreError("GitStore not initialized; call init() first")
        return self.repo, self.worktrees, self.worktrees.staging()

    def _staging_repo(self, staging: Path) -> git.Repo:
        return git.Repo(staging)

    # ── version operations ───────────────────────────────────────────

    def snapshot(self, sut_id: str) -> VersionId:
        """Commit ``suts/<sut_id>/`` from staging; idempotent when clean.

        Returns the current baseline tag commit when nothing changed.
        """
        _, _, staging = self._require()
        with self._write_lock:
            srepo = self._staging_repo(staging)
            rel = f"suts/{sut_id}"
            status = srepo.git.status("--porcelain", "--", rel)
            if not status.strip():
                tag = self._get_baseline_tag(sut_id)
                if tag is not None:
                    return VersionId(tag.commit.hexsha)
                return VersionId(srepo.head.commit.hexsha)
            return self._commit_staging(f"snapshot: {sut_id}", paths=[rel])

    def commit_candidate(self, base: VersionId, cs: Changeset, hyp_id: str) -> VersionId:
        """Apply ``cs`` on top of ``base`` in a temp worktree, commit, branch."""
        repo, worktrees, _ = self._require()
        sut_id = self._sut_from_changeset(cs)
        op_id = f"tmp-{uuid.uuid4().hex}"
        with self._write_lock:
            path = worktrees.worktree_root / op_id
            wrepo = None
            try:
                repo.git.worktree("add", str(path), str(base))
                wrepo = git.Repo(path)
                self._apply_changes(wrepo.working_tree_dir, cs.files)
                wrepo.git.add("-A")
                wrepo.index.commit(f"cand: {sut_id}/{hyp_id}")
                sha = wrepo.head.commit.hexsha
                branch = f"suts/{sut_id}/cand/{hyp_id}"
                if branch in [h.name for h in repo.heads]:
                    repo.delete_head(branch, force=True)
                repo.create_head(branch, sha)
                return VersionId(sha)
            except git.GitCommandError as exc:
                raise GitStoreError(
                    f"commit_candidate failed: {exc}",
                    sut_id=sut_id, hyp_id=hyp_id,
                ) from exc
            finally:
                if wrepo is not None:
                    wrepo.close()
                try:
                    worktrees.cleanup(op_id)
                except git.GitCommandError:
                    # Windows: open handles can block `worktree remove`;
                    # fall back to rmtree + prune.
                    shutil.rmtree(path, ignore_errors=True)
                    repo.git.worktree("prune")

    def promote(self, candidate: VersionId) -> None:
        """Fast-forward the baseline tag to ``candidate`` and sync staging."""
        repo, _, staging = self._require()
        sut_id = self._sut_of_candidate(candidate)
        with self._write_lock:
            cand_commit = repo.commit(str(candidate))
            tag = self._get_baseline_tag(sut_id)
            if tag is not None and not repo.is_ancestor(tag.commit, cand_commit):
                raise NotFastForwardError(
                    f"candidate {candidate[:8]} is not a descendant of "
                    f"baseline {tag.name}",
                    sut_id=sut_id, candidate=str(candidate), baseline=tag.name,
                )
            name = _baseline_prefix(sut_id) + f"v{self._next_tag_num(sut_id)}"
            repo.create_tag(name, ref=cand_commit, message=f"promote {sut_id}")
            self._sync_staging(staging, cand_commit.hexsha)

    def rollback(self, sut_id: str, version_id: VersionId) -> None:
        """New baseline tag at a historical commit (keep-all: old tags stay)."""
        repo, _, staging = self._require()
        with self._write_lock:
            commit = repo.commit(str(version_id))
            name = _baseline_prefix(sut_id) + f"v{self._next_tag_num(sut_id)}"
            repo.create_tag(name, ref=commit, message=f"rollback {sut_id}")
            self._sync_staging(staging, commit.hexsha)

    # ── internal helpers ─────────────────────────────────────────────

    def _commit_staging(self, message: str, paths: list[str] | None = None) -> VersionId:
        """Add (+ optionally path-filter) and commit on staging; return sha."""
        _, _, staging = self._require()
        srepo = self._staging_repo(staging)
        if paths:
            srepo.git.add("--", *paths)
        else:
            srepo.git.add("-A")
        if not srepo.index.diff("HEAD") and not srepo.git.status("--porcelain").strip():
            return VersionId(srepo.head.commit.hexsha)
        commit = srepo.index.commit(message)
        return VersionId(commit.hexsha)

    def _tag_baseline(self, sut_id: str, tag_short: str) -> None:
        """Create baseline tag ``suts/<sut>/baseline/<tag_short>`` at staging HEAD."""
        _, _, staging = self._require()
        srepo = self._staging_repo(staging)
        self.repo.create_tag(
            _baseline_prefix(sut_id) + tag_short,
            ref=srepo.head.commit,
            message=f"baseline {sut_id} {tag_short}",
        )

    def _get_baseline_tag(self, sut_id: str) -> git.TagReference | None:
        """Latest ``v<N>`` baseline tag for ``sut_id`` (max N), or None."""
        best: git.TagReference | None = None
        best_n = -1
        for tag in self.repo.tags:
            m = _BASELINE_RE.match(tag.name)
            if m and m.group("sut") == sut_id:
                n = int(m.group("n"))
                if n > best_n:
                    best, best_n = tag, n
        return best

    def _next_tag_num(self, sut_id: str) -> int:
        tag = self._get_baseline_tag(sut_id)
        return (int(_BASELINE_RE.match(tag.name).group("n")) + 1) if tag else 1

    def _sync_staging(self, staging: Path, sha: str) -> None:
        """Hard-reset the staging worktree (index + working tree) to ``sha``."""
        srepo = self._staging_repo(staging)
        srepo.git.reset("--hard", sha)

    def _sut_from_changeset(self, cs: Changeset) -> str:
        if not cs.files:
            raise GitStoreError("changeset has no file changes", hyp_id=cs.hypothesis_id)
        for fc in cs.files:
            m = _SUT_PATH_RE.match(fc.path)
            if m:
                return m.group("sut")
        raise GitStoreError(
            "cannot derive sut_id from changeset paths "
            "(expected 'suts/<sut_id>/...')",
            hyp_id=cs.hypothesis_id,
        )

    def _sut_of_candidate(self, candidate: VersionId) -> str:
        """Derive sut_id from the candidate branch pointing at ``candidate``."""
        for head in self.repo.heads:
            m = re.match(r"^suts/(?P<sut>[^/]+)/cand/", head.name)
            if m and head.commit.hexsha == str(candidate):
                return m.group("sut")
        raise GitStoreError(
            f"no candidate branch suts/<sut>/cand/* points at {candidate}",
            candidate=str(candidate),
        )

    def _apply_changes(self, tree_dir: str | Path, files: list[FileChange]) -> None:
        for fc in files:
            target = Path(tree_dir) / fc.path
            if fc.op == "delete":
                if target.exists():
                    target.unlink()
            else:
                if fc.content is None:
                    raise GitStoreError(
                        f"FileChange op={fc.op!r} requires content",
                        path=fc.path,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fc.content, encoding="utf-8")
