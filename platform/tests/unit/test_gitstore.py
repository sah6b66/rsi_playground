"""TU tests for GitStore core (DD-09 §4-5): real temp git repos."""

import git
import pytest

from rsi.core.errors import GitStoreError, NotFastForwardError
from rsi.versioning.changeset import Changeset, FileChange
from rsi.versioning.gitstore import GitStore


@pytest.fixture
def gitstore(tmp_path):
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
    )
    store.init()
    # Seed: create a SUT directory and initial baseline
    staging = store.worktrees.staging()
    sut_dir = staging / "suts" / "test-agent"
    sut_dir.mkdir(parents=True)
    (sut_dir / "system-prompt.md").write_text("# Initial prompt")
    store._commit_staging("Initial baseline")
    store._tag_baseline("test-agent", "v1")
    return store


def _cs(base, files, hyp="hyp-001"):
    return Changeset(hypothesis_id=hyp, base_version=base, files=files)


class TestGitStore:
    def test_snapshot_idempotent(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        v2 = gitstore.snapshot("test-agent")  # no changes
        assert v1 == v2  # same commit

    def test_snapshot_detects_changes(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        (gitstore.worktrees.staging() / "suts" / "test-agent" / "system-prompt.md").write_text(
            "# Edited"
        )
        v2 = gitstore.snapshot("test-agent")
        assert v2 != v1

    def test_snapshot_ignores_other_suts(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        other = gitstore.worktrees.staging() / "suts" / "other-agent"
        other.mkdir(parents=True)
        (other / "x.md").write_text("x")
        v2 = gitstore.snapshot("test-agent")
        assert v1 == v2  # change in another SUT subtree is invisible

    def test_commit_candidate(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md",
                       op="edit", content="# Updated prompt",
                       target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        assert cand != base
        # candidate branch exists and points at cand
        head = gitstore.repo.heads["suts/test-agent/cand/hyp-001"]
        assert head.commit.hexsha == cand
        # staging was not modified
        assert "Updated prompt" not in (
            gitstore.worktrees.staging() / "suts" / "test-agent" / "system-prompt.md"
        ).read_text()

    def test_commit_candidate_create_and_delete(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/tools/fix.md", op="create",
                       content="fix", target="tools", declared=True),
            FileChange(path="suts/test-agent/system-prompt.md", op="delete",
                       content=None, target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-002")
        # export via temp worktree to verify tree contents
        path = gitstore.worktrees.worktree_root / "tmp-verify"
        gitstore.repo.git.worktree("add", str(path), str(cand))
        try:
            assert (path / "suts" / "test-agent" / "tools" / "fix.md").read_text() == "fix"
            assert not (path / "suts" / "test-agent" / "system-prompt.md").exists()
        finally:
            import shutil
            try:
                gitstore.repo.git.worktree("remove", str(path), "--force")
            except git.GitCommandError:
                shutil.rmtree(path, ignore_errors=True)
                gitstore.repo.git.worktree("prune")

    def test_commit_candidate_no_sut_in_path_raises(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="etc/config.md", op="create", content="x",
                       target="misc", declared=True),
        ])
        with pytest.raises(GitStoreError):
            gitstore.commit_candidate(base, cs, "hyp-001")

    def test_promote_fast_forward(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# V2", target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        gitstore.promote(cand)  # should not raise
        # new baseline tag exists and staging synced
        assert "suts/test-agent/baseline/v2" in [t.name for t in gitstore.repo.tags]
        assert (gitstore.worktrees.staging() / "suts" / "test-agent"
                / "system-prompt.md").read_text() == "# V2"
        # snapshot now idempotent at new baseline
        assert gitstore.snapshot("test-agent") == cand

    def test_promote_non_fast_forward_raises(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs1 = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# A", target="system_prompt", declared=True),
        ])
        cand_a = gitstore.commit_candidate(base, cs1, "hyp-a")
        cs2 = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# B", target="system_prompt", declared=True),
        ])
        cand_b = gitstore.commit_candidate(base, cs2, "hyp-b")
        gitstore.promote(cand_a)  # baseline becomes v2 = cand_a
        with pytest.raises(NotFastForwardError):
            gitstore.promote(cand_b)  # sibling, not descendant

    def test_rollback_keeps_history(self, gitstore):
        base = gitstore.snapshot("test-agent")
        gitstore.rollback("test-agent", base)
        # Old tag should still exist (keep-all)
        names = [t.name for t in gitstore.repo.tags]
        assert "suts/test-agent/baseline/v1" in names
        assert "suts/test-agent/baseline/v2" in names
        # both tags point at the same historical commit
        assert gitstore.repo.tags["suts/test-agent/baseline/v1"].commit.hexsha == base
        assert gitstore.repo.tags["suts/test-agent/baseline/v2"].commit.hexsha == base

    def test_rollback_syncs_staging(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# V2", target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        gitstore.promote(cand)
        gitstore.rollback("test-agent", base)
        assert (gitstore.worktrees.staging() / "suts" / "test-agent"
                / "system-prompt.md").read_text() == "# Initial prompt"

    def test_tag_numbering_increments(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# N", target="system_prompt", declared=True),
        ])
        for i in range(3):
            base = gitstore.snapshot("test-agent")
            cs = _cs(base, [
                FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                           content=f"# v{i}", target="system_prompt", declared=True),
            ])
            cand = gitstore.commit_candidate(base, cs, f"hyp-{i}")
            gitstore.promote(cand)
        names = sorted(t.name for t in gitstore.repo.tags)
        assert names == [f"suts/test-agent/baseline/v{i}" for i in (1, 2, 3, 4)]

    def test_uninitialized_raises(self, tmp_path):
        store = GitStore(tmp_path / "r.git", tmp_path / "wt")
        with pytest.raises(GitStoreError):
            store.snapshot("test-agent")
