"""Unit tests for rsi.versioning.worktrees (DD-09 §5.1).

Uses real temp git repositories — no mocking of git operations.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import git
import pytest

from rsi.versioning.worktrees import STAGING_DIR, Worktrees


class TestWorktrees:
    def test_staging_persists(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        staging = wt.staging()
        assert staging.exists()
        assert (staging / ".git").exists()  # it's a worktree

    def test_staging_is_idempotent(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        s1 = wt.staging()
        s2 = wt.staging()
        assert s1 == s2
        assert s2.exists()

    def test_staging_reuses_existing_bare_repo(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        wt.staging()
        wt2 = Worktrees(tmp_path / "wt")
        assert wt2.repo.git_dir == wt.repo.git_dir
        wt2.staging()  # no error on re-open

    def test_temp_lifecycle(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        temp = wt.temp("op-123")
        assert temp.exists()
        assert (temp / ".git").exists()
        wt.cleanup("op-123")
        assert not temp.exists()

    def test_temp_checkout_of_head(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        staging = wt.staging()
        (staging / "hello.txt").write_text("hi")
        staging_repo = git.Repo(staging)
        staging_repo.git.add("-A")
        staging_repo.git.commit("-m", "add file")
        temp = wt.temp("op-abc")
        assert (temp / "hello.txt").exists()  # HEAD content is checked out
        wt.cleanup("op-abc")

    def test_temp_conflict_raises(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        wt.temp("dup")
        with pytest.raises(FileExistsError):
            wt.temp("dup")
        wt.cleanup("dup")

    def test_cleanup_missing_is_noop(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        wt.cleanup("never-existed")  # should not raise

    def test_bare_repo_created(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        assert wt.repo.bare

    def test_fresh_repo_has_initial_commit(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        assert wt.repo.head.is_valid()
        assert wt.repo.head.commit.message.strip()

    def test_cleanup_stale_removes_orphans(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        wt.staging()
        orphan = tmp_path / "wt" / "op-dead"
        orphan.mkdir()
        (orphan / "junk").write_text("x")
        live = wt.temp("op-live")
        removed = wt.cleanup_stale()
        assert "op-dead" in removed
        assert not orphan.exists()
        assert live.exists()  # registered worktrees survive
        assert wt._staging_path().exists()  # staging survives
        wt.cleanup("op-live")

    def test_cleanup_stale_after_real_temp(self, tmp_path):
        wt = Worktrees(tmp_path / "wt")
        t = wt.temp("op-x")
        wt.repo.git.worktree("remove", str(t), "--force")
        # dir gone but metadata may linger; cleanup_stale should not recreate it
        removed = wt.cleanup_stale()
        assert "op-x" not in removed

    def test_explicit_repo_injection(self, tmp_path):
        bare = git.Repo.init(tmp_path / "custom.git", bare=True)
        wt = Worktrees(tmp_path / "wt", repo=bare)
        assert wt.repo is bare
        staging = wt.staging()
        assert staging.exists()
