"""B1-1：evalset 变更端点（update/:freeze/:assign）落盘缺口.

F3 只覆盖了 create/:import/:clone/delete；update（version 自增）、
freeze、assign 改记录后不 persist——重启即回退。断言三端点变更后
JSON 对象库文件内容与内存一致。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.settings import Settings

pytestmark = pytest.mark.asyncio


def make_services(extra: dict | None = None) -> dict:
    # 本仓模式：每测试文件自带（参照 tests/unit/test_web_resources_api.py:93）
    from rsi.api.deps import default_stores

    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "settings": Settings(),
        "stores": default_stores(Settings()),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            }
        },
    }
    services.update(extra or {})
    return services


def _seed(store: dict) -> None:
    store["es-1"] = {
        "id": "es-1", "name": "ensp-spineleaf", "purpose": "iterate",
        "tasks": [], "description": "d0", "env_yaml": "", "configs": {},
        "frozen": False, "version": 1, "created_at": "2026-01-01T00:00:00Z",
        "versions": [],
    }


def _disk(tmp_path: Path, es_id: str = "es-1") -> dict:
    f = tmp_path / "storage" / "object-store" / "evalsets" / f"{es_id}.json"
    return json.loads(f.read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    services = make_services()
    app = create_app(services)
    _seed(app.state.stores["evalsets"])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


async def _hdr(client) -> dict:
    r = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_update_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.put(
        "/api/v1/evalsets/es-1", json={"description": "d1"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert _disk(tmp_path)["description"] == "d1"
    assert _disk(tmp_path)["version"] == 2


async def test_freeze_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.post("/api/v1/evalsets/es-1:freeze", headers=h)
    assert r.status_code == 200, r.text
    assert _disk(tmp_path)["frozen"] is True


async def test_assign_evalset_persists(client, tmp_path):
    h = await _hdr(client)
    r = await client.post(
        "/api/v1/evalsets/es-1:assign", json={"purpose": "holdout"}, headers=h
    )
    assert r.status_code == 204, r.text
    assert _disk(tmp_path)["purpose"] == "holdout"
