"""B2-T8：llm_calls.estimated 落列 + 旧库（缺列）ALTER 自迁移.

FR5.6 预算记账的真实性标记：usage 真实回报（handler 带 usage dict）→
estimated=False；字符估算（mock/无 usage）→ estimated=True。SQLModel
create_all 只建新库不迁移旧库——Accountant._persist 捕 "no such column"
后 ALTER TABLE 重试一次（幂等：列已存在则插入成功、永不触发 ALTER）。
"""

from __future__ import annotations

from functools import partial

import pytest
from sqlmodel import SQLModel, select

from rsi.db import create_engine, get_session
from rsi.llm.accounting import Accountant

# 导入即注册全部 SQLModel 表（create_all 需要）
import rsi.db.tables  # noqa: F401


def _factory(engine):
    return partial(get_session, engine)


def test_estimated_persisted_true_and_false(tmp_path):
    from rsi.db.tables import LlmCall

    engine = create_engine(f"sqlite:///{(tmp_path / 'llm.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    acc = Accountant(session_factory=_factory(engine))
    acc.record("judge", "m", "v1", 10, 5, 3, estimated=True)
    acc.record("critic", "m", "v1", 7, 2, 1, estimated=False)
    with get_session(engine) as session:
        rows = session.exec(select(LlmCall).order_by(LlmCall.id)).all()
    assert [bool(r.estimated) for r in rows] == [True, False]
    # 内存台账同样带 estimated 标记
    assert [r["estimated"] for r in acc.records()] == [True, False]
    assert rows[0].component == "judge"  # 既有列不受影响


def test_old_db_missing_column_self_migrates_via_alter(tmp_path):
    """预建缺 estimated 列的旧库 → 首次写入触发 ALTER → 行落库 estimated==1."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    legacy_cols = (
        "id INTEGER PRIMARY KEY, ts TEXT NOT NULL, component TEXT NOT NULL, "
        "model TEXT NOT NULL, prompt_version TEXT NOT NULL, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd FLOAT, latency_ms INTEGER, "
        "run_id TEXT, batch_id TEXT"
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE TABLE llm_calls ({legacy_cols})")

    acc = Accountant(session_factory=_factory(engine))
    # 不得抛异常（DB 写入 try/except + warning 铁律），且 ALTER 后写入成功
    acc.record("judge", "glm-5.1", "v1", 1, 1, 1, estimated=True)
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT component, estimated FROM llm_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "judge"
    assert int(row[1]) == 1


def test_alter_idempotent_column_already_present(tmp_path):
    """列已存在（新库）→ 插入永不触发 ALTER；连续多条写入稳定成功."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        before = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(llm_calls)")
        }
    assert "estimated" in before  # 新库 create_all 直接带列

    acc = Accountant(session_factory=_factory(engine))
    for i in range(3):
        acc.record("attribution", "m", "v1", 1, 1, 1, estimated=bool(i % 2))
    with engine.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT estimated FROM llm_calls ORDER BY id"
        ).fetchall()
    assert [int(r[0]) for r in rows] == [0, 1, 0]


def test_record_without_session_factory_still_tallies():
    """无 session_factory：内存台账照记（既有行为回归，estimated 入台账）."""
    acc = Accountant(session_factory=None)
    acc.record("judge", "m", "v1", 1, 1, 1, estimated=True)
    assert acc.records()[0]["estimated"] is True
    assert acc.sum_usage()["calls"] == 1
