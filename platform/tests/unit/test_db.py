"""Tests for the database layer (Task 5)."""

import asyncio

import pytest
from sqlalchemy import text

from rsi.db import create_engine, get_session, init_db, run_db


class TestDatabase:
    def test_init_creates_all_tables(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")
        with get_session(engine) as s:
            result = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            tables = {row[0] for row in result}
        expected = {
            "users", "credentials", "audit_log", "suts", "config_versions",
            "evalsets", "eval_tasks", "labs", "lab_snapshots",
            "optimization_profiles", "iterations", "batches", "runs",
            "instrumentation_reports", "trace_files", "trace_events_index",
            "judgments", "hypotheses", "experiments", "noise_bands",
            "llm_calls", "checkpoints",
        }
        assert expected <= tables
        assert len(tables - {"sqlite_sequence"}) >= 22

    def test_wal_mode(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        with get_session(engine) as s:
            mode = s.execute(text("PRAGMA journal_mode")).scalar()
        assert mode == "wal"

    def test_busy_timeout(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        with get_session(engine) as s:
            timeout = s.execute(text("PRAGMA busy_timeout")).scalar()
        assert timeout == 5000

    def test_admin_seed(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hashed")
        with get_session(engine) as s:
            from rsi.db.tables import User
            user = s.query(User).filter_by(username="admin").first()
        assert user is not None
        assert user.role == "admin"
        assert user.password_hash == "hashed"
        assert user.disabled == 0

    def test_admin_seed_idempotent(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="h1")
        init_db(engine, admin_user="admin", admin_password_hash="h2")
        with get_session(engine) as s:
            from rsi.db.tables import User
            users = s.query(User).filter_by(username="admin").all()
        assert len(users) == 1
        assert users[0].password_hash == "h2"

    def test_foreign_keys_present(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")
        with get_session(engine) as s:
            fks = s.execute(
                text("PRAGMA foreign_key_list(config_versions)")
            ).fetchall()
        assert any(row[2] == "suts" for row in fks)

    def test_indexes_created(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")
        with get_session(engine) as s:
            idx = {
                row[0]
                for row in s.execute(
                    text("SELECT name FROM sqlite_master WHERE type='index'")
                )
            }
        for name in (
            "ix_runs_batch_id",
            "ix_runs_cache_key",
            "ix_trace_events_index_event_type_run_id",
            "ix_llm_calls_batch_id",
            "ix_audit_log_ts",
        ):
            assert name in idx

    def test_get_session_commits_on_success(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")
        with get_session(engine) as s:
            s.execute(
                text(
                    "INSERT INTO credentials (id, name, kind, cipher_blob, created_at) "
                    "VALUES ('c1', 'nms', 'nms_api', x'00', '2026-01-01T00:00:00Z')"
                )
            )
        with get_session(engine) as s:
            count = s.execute(text("SELECT COUNT(*) FROM credentials")).scalar()
        assert count == 1

    def test_run_db_async(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(engine, admin_user="admin", admin_password_hash="hash")

        def count_users(s):
            return s.execute(text("SELECT COUNT(*) FROM users")).scalar()

        result = asyncio.run(run_db(lambda s: count_users(s), engine))
        assert result == 1
