"""F1：首启动初始化入口 python -m rsi.init（测试先行）。

run_init 在全新 RSI_HOME 下应完成三件事（INSTALL.md 承诺而生产路径
缺位的部分）：建表 + argon2 种管理员 + Git 裸仓初始化；且幂等可重跑
——重跑不得覆盖既有管理员密码（A-11①）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from sqlmodel import select

from rsi.settings import Settings


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    monkeypatch.delenv("RSI_INTERNAL_SETTINGS", raising=False)
    return tmp_path


def _db_file(home: Path) -> Path:
    settings = Settings()
    url = settings.database_url
    assert url.startswith("sqlite:///"), f"expected sqlite url, got {url}"
    return Path(url.removeprefix("sqlite:///"))


def _table_count(db: Path) -> int:
    con = sqlite3.connect(db)
    try:
        return len(
            con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
    finally:
        con.close()


def _stored_hash(home: Path, username: str = "admin") -> str:
    from rsi.db import create_engine, get_session
    from rsi.db.tables import User

    engine = create_engine(Settings().database_url)
    try:
        with get_session(engine) as session:
            row = session.exec(
                select(User).where(User.username == username)
            ).first()
            assert row is not None, f"user {username!r} missing"
            return row.password_hash or ""
    finally:
        engine.dispose()


def test_fresh_init_creates_tables_admin_and_git(fresh_home: Path) -> None:
    from rsi.db import ALL_TABLES
    from rsi.init import run_init

    report = run_init(admin_password="S3cret-pass-1")

    db = _db_file(fresh_home)
    assert db.exists()
    assert _table_count(db) == len(ALL_TABLES)
    assert PasswordHasher().verify(_stored_hash(fresh_home), "S3cret-pass-1")
    assert (Settings().git_repo_path / "HEAD").exists()
    assert report["admin_user"] == "admin"
    assert report["admin_password_effective"] is True


def test_rerun_keeps_existing_admin_password(fresh_home: Path) -> None:
    from rsi.init import run_init

    run_init(admin_password="first-pass-123")
    report = run_init(admin_password="second-pass-456")

    assert report["admin_password_effective"] is False
    assert report["admin_password"] is None
    hasher = PasswordHasher()
    stored = _stored_hash(fresh_home)
    assert hasher.verify(stored, "first-pass-123")
    with pytest.raises(Exception):  # noqa: B017 - argon2 mismatch raises
        hasher.verify(stored, "second-pass-456")


def test_generated_password_reported_only_when_effective(
    fresh_home: Path,
) -> None:
    from rsi.init import run_init

    report = run_init()  # 未提供密码 → 随机生成
    assert report["admin_password_generated"] is True
    assert report["admin_password_effective"] is True
    assert isinstance(report["admin_password"], str) and len(
        report["admin_password"]
    ) >= 16
    assert PasswordHasher().verify(
        _stored_hash(fresh_home), report["admin_password"]
    )


def test_cli_main_smoke(
    fresh_home: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rsi.init import main

    monkeypatch.setenv("RSI_ADMIN_PASSWORD", "Cli-pass-9x")
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "admin" in out
    assert "Git" in out
    assert "Cli-pass-9x" not in out  # 显式传入的密码不得回显
    assert PasswordHasher().verify(_stored_hash(fresh_home), "Cli-pass-9x")
