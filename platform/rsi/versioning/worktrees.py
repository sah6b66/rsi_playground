"""Worktree lifecycle management (DD-09 §5.1, ADR-15).

Manages a persistent *staging* worktree plus per-operation temporary
worktrees under ``worktree_root``, all backed by a single bare repository::

    var/git/platform-repo.git/     # bare repo (all objects)
    var/git/wt/staging/            # persistent staging worktree
    var/git/wt/<op-id>/            # temp worktrees (try/finally cleanup)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import git

STAGING_DIR = "staging"
_BARE_NAME = "platform-repo.git"
_INIT_COMMIT_MSG = "chore: initial empty commit"


class Worktrees:
    """Create/remove git worktrees backed by a bare repository.

    The bare repository lives at ``worktree_root.parent / platform-repo.git``
    (created on demand if not supplied). A fresh bare repo has no commits, so
    an initial empty commit is created to make worktrees possible.
    """

    def __init__(self, worktree_root: Path, repo: git.Repo | None = None) -> None:
        self.worktree_root = Path(worktree_root)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        if repo is None:
            bare_path = self.worktree_root.parent / _BARE_NAME
            if bare_path.exists() and (bare_path / "HEAD").exists():
                repo = git.Repo(bare_path)
            else:
                repo = git.Repo.init(bare_path, bare=True)
        self.repo = repo
        self._ensure_initial_commit()
        self._ensure_identity()

    # ── internal helpers ──────────────────────────────────────────────

    def _ensure_initial_commit(self) -> None:
        """A worktree needs at least one commit; create one on a fresh repo."""
        if not self.repo.heads:
            config_writer = self.repo.config_writer()
            config_writer.set_value("user", "name", "rsi-platform")
            config_writer.set_value("user", "email", "platform@rsi.local")
            config_writer.release()
            self.repo.index.commit(_INIT_COMMIT_MSG)

    def _ensure_identity(self) -> None:
        with self.repo.config_writer() as cw:
            if not cw.has_option("user", "name"):
                cw.set_value("user", "name", "rsi-platform")
                cw.set_value("user", "email", "platform@rsi.local")

    def _staging_path(self) -> Path:
        return self.worktree_root / STAGING_DIR

    def _temp_path(self, op_id: str) -> Path:
        return self.worktree_root / op_id

    def _add_worktree(self, path: Path, commitish: str = "HEAD") -> Path:
        self.repo.git.worktree("add", str(path), commitish)
        return path

    def _registered_worktrees(self) -> list[Path]:
        """Paths of all registered worktrees (including the main bare dir)."""
        out = self.repo.git.worktree("list", "--porcelain")
        return [
            Path(line[len("worktree ") :])
            for line in out.splitlines()
            if line.startswith("worktree ")
        ]

    # ── public API ────────────────────────────────────────────────────

    def staging(self) -> Path:
        """Return the persistent staging worktree path, creating it if needed."""
        staging = self._staging_path()
        if not staging.exists():
            # Check out the bare repo's active branch explicitly (a bare
            # `worktree add` would create a new branch named "staging").
            branch = self.repo.active_branch.name
            self.repo.git.worktree("add", str(staging), branch)
        return staging

    def temp(self, op_id: str) -> Path:
        """Create a temporary worktree for one operation (checkout HEAD)."""
        path = self._temp_path(op_id)
        if path.exists():
            raise FileExistsError(f"temp worktree already exists: {path}")
        return self._add_worktree(path, "HEAD")

    def cleanup(self, op_id: str) -> None:
        """Remove the temp worktree for ``op_id`` and prune worktree metadata."""
        path = self._temp_path(op_id)
        if path.exists():
            self.repo.git.worktree("remove", str(path), "--force")
        self.repo.git.worktree("prune")

    def cleanup_stale(self) -> list[str]:
        """Remove orphaned directories under worktree_root (not staging, not
        registered worktrees) and prune worktree metadata.

        Returns the list of removed directory names.
        """
        self.repo.git.worktree("prune")
        staging = self._staging_path().resolve()
        registered = {p.resolve() for p in self._registered_worktrees()}
        removed: list[str] = []
        for entry in sorted(self.worktree_root.iterdir()):
            if not entry.is_dir() or entry.resolve() == staging:
                continue
            if entry.resolve() in registered:
                continue
            if entry.name.startswith("."):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        self.repo.git.worktree("prune")
        return removed
