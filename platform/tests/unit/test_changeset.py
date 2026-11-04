"""Unit tests for rsi.versioning.changeset (DD-09 §3)."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from pydantic import ValidationError

from rsi.versioning.changeset import Changeset, FileChange, VersionId


class TestFileChange:
    def test_valid_create(self):
        fc = FileChange(
            path="sut_a/config.yaml", op="create", content="k: v", target="param-x", declared=True
        )
        assert fc.path == "sut_a/config.yaml"
        assert fc.op == "create"
        assert fc.content == "k: v"
        assert fc.target == "param-x"
        assert fc.declared is True

    def test_delete_has_no_content(self):
        fc = FileChange(path="a.py", op="delete", target="param-x", declared=True)
        assert fc.content is None

    def test_invalid_op_rejected(self):
        with pytest.raises(ValidationError):
            FileChange(path="a.py", op="rename", target="t", declared=True)

    def test_missing_fields_rejected(self):
        with pytest.raises(ValidationError):
            FileChange(path="a.py", op="create")


class TestChangeset:
    def test_minimal_changeset(self):
        cs = Changeset(hypothesis_id="hyp-1", base_version=VersionId("0" * 40))
        assert cs.files == []

    def test_full_changeset(self):
        fc = FileChange(path="a.py", op="edit", content="x=1", target="t", declared=True)
        cs = Changeset(hypothesis_id="hyp-2", base_version="a" * 40, files=[fc])
        assert len(cs.files) == 1
        assert cs.files[0].op == "edit"
        assert isinstance(cs.base_version, str)

    def test_version_id_is_str(self):
        assert VersionId("b" * 40) == "b" * 40
