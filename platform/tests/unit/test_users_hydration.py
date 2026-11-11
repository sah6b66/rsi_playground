"""F1 补充：users store 启动水合 + 用户管理落库（重启不丢账号）。

登录校验只看内存 users store（auth.py），DB 表此前后从不回灌——
python -m rsi.init 种的 admin 到不了运行时、API 创建的用户重启即失。
本组用例锁定：init 种子 → 登录可用；API 建号 → 重新装配后仍在；
无表环境诚实降级为空 store。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from rsi.settings import Settings


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    monkeypatch.delenv("RSI_INTERNAL_SETTINGS", raising=False)
    return tmp_path


def _client(services):
    from rsi.api.app import create_app

    app = create_app(services)
    return TestClient(app, raise_server_exceptions=False)


def test_init_seeded_admin_can_login(fresh_home: Path) -> None:
    from rsi.bootstrap import build_services
    from rsi.init import run_init

    run_init(admin_password="Boot-Pass-1")
    services = build_services()
    with _client(services) as c:
        resp = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "Boot-Pass-1"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token"] and body["role"] == "admin"


def test_created_user_survives_reassembly(fresh_home: Path) -> None:
    from rsi.bootstrap import build_services
    from rsi.init import run_init

    run_init(admin_password="Boot-Pass-1")
    services = build_services()
    with _client(services) as c:
        login = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "Boot-Pass-1"},
        ).json()
        resp = c.post(
            "/api/v1/users",
            headers={"Authorization": f"Bearer {login['token']}"},
            json={
                "username": "alice",
                "password": "Alice-Pass-9",
                "role": "researcher",
            },
        )
    assert resp.status_code == 201, resp.text

    services2 = build_services()  # 模拟重启（同一 RSI_HOME）
    with _client(services2) as c:
        resp = c.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "Alice-Pass-9"},
        )
    assert resp.status_code == 200, resp.text


def test_missing_table_degrades_to_empty(fresh_home: Path) -> None:
    """未跑 init 的全新环境：水合诚实降级，进程不崩。"""
    from rsi.bootstrap import build_services

    services = build_services()
    assert services["users"] == {}
    with _client(services) as c:
        resp = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "whatever"},
        )
    assert resp.status_code == 401
