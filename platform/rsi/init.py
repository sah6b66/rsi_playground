"""首启动初始化入口：``python -m rsi.init``（F1 / INSTALL.md 步骤）。

生产装配路径（``build_services`` → ``bootstrap_database(init=False)``）
刻意不做任何写入——建表、管理员种子与 Git 裸仓初始化集中在本 CLI，
幂等可重跑：重跑不会覆盖既有管理员密码（A-11①）。

用法::

    python -m rsi.init [--username admin] [--password <pwd>]

密码来源优先级：``--password`` > ``RSI_ADMIN_PASSWORD`` > 随机生成
（仅当随机密码真正生效时打印一次）。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import Any

from rsi.settings import Settings


def run_init(
    settings: Settings | None = None,
    *,
    admin_user: str = "admin",
    admin_password: str | None = None,
) -> dict[str, Any]:
    """Initialize DB tables + admin account + version-control bare repo.

    Returns a report dict; never raises on the happy path. Safe to re-run.
    """
    from argon2 import PasswordHasher
    from sqlmodel import select

    from rsi.db import ALL_TABLES, create_engine, get_session, init_db
    from rsi.db.tables import User
    from rsi.versioning.gitstore import GitStore

    settings = settings or Settings()
    hasher = PasswordHasher()

    generated = admin_password is None
    password = admin_password or secrets.token_urlsafe(18)

    engine = create_engine(settings.database_url)
    try:
        init_db(engine, admin_user, hasher.hash(password))

        effective = False
        with get_session(engine) as session:
            row = session.exec(
                select(User).where(User.username == admin_user)
            ).first()
            stored = row.password_hash if row else ""
        if stored:
            try:
                effective = hasher.verify(stored, password)
            except Exception:  # noqa: BLE001 - invalid/rehashed -> not ours
                effective = False
    finally:
        pruned = _prune_login_audit(engine)
        if pruned:
            print(f"✓ 登录审计瘦身: 清理 {pruned} 条 user.login 历史行（W10）")
        engine.dispose()

    git_store = GitStore(
        repo_path=settings.git_repo_path,
        worktree_root=settings.git_worktrees_dir,
    )
    git_store.init()

    return {
        "database_url": settings.database_url,
        "tables": len(ALL_TABLES),
        "admin_user": admin_user,
        "admin_password_effective": effective,
        "admin_password_generated": generated,
        # 仅"随机生成且真正生效"的密码回显一次；显式传入的不回显。
        "admin_password": password if (generated and effective) else None,
        "git_repo_path": str(settings.git_repo_path),
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台 GBK 防御
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="python -m rsi.init",
        description="首启动初始化：建表 + 管理员账号 + Git 裸仓（幂等）",
    )
    parser.add_argument("--username", default="admin", help="管理员用户名")
    parser.add_argument(
        "--password",
        default=None,
        help="管理员密码（缺省读 RSI_ADMIN_PASSWORD，再缺省随机生成并打印一次）",
    )
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("RSI_ADMIN_PASSWORD") or None

    from rsi.versioning.gitstore import GitStoreError

    try:
        rep = run_init(admin_user=args.username, admin_password=password)
    except GitStoreError as exc:
        print(f"[init] Git 仓库初始化失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一兜底
        print(f"[init] 初始化失败: {exc}", file=sys.stderr)
        return 1

    print(f"✓ 数据库已就绪（{rep['tables']} 张表）: {rep['database_url']}")
    if rep["admin_password_effective"]:
        print(f"✓ 管理员账号 {rep['admin_user']} 已就绪（密码校验通过）")
        if rep["admin_password_generated"] and rep["admin_password"]:
            print(
                f"  随机生成密码: {rep['admin_password']} "
                "（仅本次显示，请立即保存；可用 WEB 用户页修改）"
            )
    else:
        print(
            f"⚠ 管理员 {rep['admin_user']} 已存在，未改动其密码 "
            "（A-11①：种子仅首次生效）"
        )
    print(f"✓ Git 裸仓已就绪: {rep['git_repo_path']}")
    return 0


def _prune_login_audit(engine) -> int:
    """F4/W10：user.login 审计只保留最近 200 行（历史灌水体量治理）。"""
    from sqlalchemy import text as _text

    try:
        with engine.connect() as conn:
            res = conn.execute(_text(
                "DELETE FROM audit_log WHERE action='user.login' AND id <= ("
                " SELECT MIN(id) FROM ("
                "  SELECT id FROM audit_log WHERE action='user.login'"
                "  ORDER BY id DESC LIMIT 200))"))
            conn.commit()
            return res.rowcount or 0
    except Exception:  # noqa: BLE001 - 清理尽力而为
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
