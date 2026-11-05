"""TU tests for GitStore extras (DD-09 §4): export/diff/lineage/freeze/files.

Extends the T9 test fixtures (real temp git repos).
"""

import json

import pytest

from rsi.core.errors import GitStoreError
from rsi.versioning.changeset import Changeset, FileChange
from rsi.versioning.gitstore import GitStore


@pytest.fixture
def gitstore(tmp_path):
    store = GitStore(
        repo_path=tmp_path / "repo.git",
        worktree_root=tmp_path / "wt",
    )
    store.init()
    staging = store.worktrees.staging()
    sut_dir = staging / "suts" / "test-agent"
    sut_dir.mkdir(parents=True)
    (sut_dir / "system-prompt.md").write_text("# Initial prompt")
    store._commit_staging("Initial baseline")
    store._tag_baseline("test-agent", "v1")
    return store


def _cs(base, files, hyp="hyp-001"):
    return Changeset(hypothesis_id=hyp, base_version=base, files=files)


class TestExport:
    def test_export(self, gitstore, tmp_path):
        base = gitstore.snapshot("test-agent")
        dest = gitstore.export(base, tmp_path / "export")
        assert (dest / "suts" / "test-agent" / "system-prompt.md").exists()
        assert not (dest / ".git").exists()

    def test_export_specific_version(self, gitstore, tmp_path):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# Updated", target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        dest = gitstore.export(base, tmp_path / "v1")
        assert "# Initial prompt" in (dest / "suts" / "test-agent" / "system-prompt.md").read_text()
        dest2 = gitstore.export(cand, tmp_path / "v2")
        assert "# Updated" in (dest2 / "suts" / "test-agent" / "system-prompt.md").read_text()


class TestDiff:
    def test_diff_edit(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        (gitstore.worktrees.staging() / "suts" / "test-agent" / "system-prompt.md").write_text(
            "# Edited prompt"
        )
        v2 = gitstore._commit_staging("edit prompt")
        diffs = gitstore.diff(v1, v2)
        assert len(diffs) == 1
        assert diffs[0].path.endswith("system-prompt.md")
        assert diffs[0].op == "edit"
        assert "# Edited prompt" in diffs[0].patch

    def test_diff_create_and_delete(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        staging = gitstore.worktrees.staging()
        (staging / "suts" / "test-agent" / "tools" / "fix.md").parent.mkdir(parents=True)
        (staging / "suts" / "test-agent" / "tools" / "fix.md").write_text("fix")
        v2 = gitstore._commit_staging("add file")
        diffs = gitstore.diff(v1, v2)
        assert any(d.op == "create" and "fix.md" in d.path for d in diffs)
        back = gitstore.diff(v2, v1)
        assert any(d.op == "delete" and "fix.md" in d.path for d in back)


class TestLineage:
    def test_lineage_baseline_and_candidate(self, gitstore):
        base = gitstore.snapshot("test-agent")
        cs = _cs(base, [
            FileChange(path="suts/test-agent/system-prompt.md", op="edit",
                       content="# Cand", target="system_prompt", declared=True),
        ])
        cand = gitstore.commit_candidate(base, cs, "hyp-001")
        gitstore.promote(cand)
        graph = gitstore.lineage("test-agent")
        labels = {n.label for n in graph.nodes}
        assert "suts/test-agent/baseline/v1" in labels
        assert "suts/test-agent/baseline/v2" in labels
        assert "suts/test-agent/cand/hyp-001" in labels
        cand_node = next(n for n in graph.nodes if n.label.endswith("cand/hyp-001"))
        assert cand_node.kind == "candidate"
        assert cand_node.experiment_ids == ["hyp-001"]
        # edge from base to candidate commit
        assert (str(base), str(cand)) in graph.edges


class TestFreeze:
    def test_freeze_evalset(self, gitstore):
        staging = gitstore.worktrees.staging()
        es_dir = staging / "evalsets" / "test-es"
        es_dir.mkdir(parents=True)
        (es_dir / "env.yaml").write_text("name: test")
        (es_dir / "cases.yaml").write_text("cases: []")
        gitstore._commit_staging("add evalset")
        record = gitstore.freeze_evalset("test-es")
        assert record.evalset_id == "test-es"
        assert record.content_hash is not None
        assert len(record.content_hash) == 64
        assert record.frozen_at
        tag_names = [t.name for t in gitstore.repo.tags]
        assert "evalsets/test-es/freeze/v1" in tag_names
        # second freeze with identical content is idempotent: same tag reused
        record2 = gitstore.freeze_evalset("test-es")
        assert record2.content_hash == record.content_hash
        assert "evalsets/test-es/freeze/v2" not in [
            t.name for t in gitstore.repo.tags
        ]
        # changed content -> a new freeze tag
        (es_dir / "cases.yaml").write_text("cases: [c1]")
        gitstore._commit_staging("edit evalset")
        record3 = gitstore.freeze_evalset("test-es")
        assert record3.content_hash != record.content_hash
        assert "evalsets/test-es/freeze/v2" in [t.name for t in gitstore.repo.tags]

    def test_freeze_missing_evalset(self, gitstore):
        with pytest.raises(GitStoreError):
            gitstore.freeze_evalset("no-such-es")

    def test_verify_freeze_no_manifest(self, gitstore):
        assert gitstore.verify_freeze() is True

    def test_verify_freeze_detects_tamper(self, gitstore):
        import hashlib
        staging = gitstore.worktrees.staging()
        judge = staging / "judge"
        judge.mkdir(parents=True)
        f = judge / "prompt.txt"
        f.write_text("frozen")
        digest = hashlib.sha256(b"frozen").hexdigest()
        (judge / "freeze.manifest").write_text(
            json.dumps({"judge/prompt.txt": digest})
        )
        gitstore._commit_staging("add freeze manifest")
        assert gitstore.verify_freeze() is True
        f.write_text("tampered")
        assert gitstore.verify_freeze() is False


class TestFiles:
    def test_list_files_staging(self, gitstore):
        files = gitstore.list_files("test-agent")
        assert files == {"system-prompt.md": "# Initial prompt"}

    def test_list_files_version(self, gitstore):
        v1 = gitstore.snapshot("test-agent")
        (gitstore.worktrees.staging() / "suts" / "test-agent" / "system-prompt.md").write_text(
            "# Edited"
        )
        gitstore._commit_staging("edit")
        # historical version still shows original content
        assert gitstore.list_files("test-agent", v1) == {
            "system-prompt.md": "# Initial prompt"
        }

    def test_list_files_missing_sut(self, gitstore):
        with pytest.raises(GitStoreError):
            gitstore.list_files("no-such-agent")

    def test_write_file_no_commit(self, gitstore):
        gitstore.write_file("test-agent", "tools/fix.md", "fix content")
        files = gitstore.list_files("test-agent")
        assert files["tools/fix.md"] == "fix content"

    def test_write_file_with_commit(self, gitstore):
        from git import Repo
        gitstore.snapshot("test-agent")
        head_before = Repo(gitstore.worktrees.staging()).head.commit.hexsha
        gitstore.write_file("test-agent", "notes.md", "n1", commit=True, message="add notes")
        head_after = Repo(gitstore.worktrees.staging()).head.commit.hexsha
        assert head_before != head_after  # a new commit/version was created
        assert gitstore.list_files("test-agent")["notes.md"] == "n1"
