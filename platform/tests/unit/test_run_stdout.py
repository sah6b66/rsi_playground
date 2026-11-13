"""F1/C1: SUT stdout 落 artifacts + GET /runs/{id}/stdout 端点."""
from pathlib import Path

import pytest

from rsi.orchestrator.batch import persist_stdout


def test_persist_stdout_writes_artifact(tmp_path):
    out = persist_stdout("run-x", "answer text\n<FINAL_JSON>{}</FINAL_JSON>", tmp_path)
    assert out is not None
    p = tmp_path / "run-x" / "stdout.txt"
    assert p.read_text(encoding="utf-8").startswith("answer text")
    assert out == p


def test_persist_stdout_empty_is_noop(tmp_path):
    assert persist_stdout("run-y", "", tmp_path) is None
    assert persist_stdout("run-y", None, tmp_path) is None  # type: ignore[arg-type]
    assert not (tmp_path / "run-y").exists()


def test_persist_stdout_bad_dir_does_not_raise(tmp_path):
    # 目录不可写等异常不阻断主流程
    persist_stdout("run-z", "x", tmp_path / "nonexistent-parent" / "a")
