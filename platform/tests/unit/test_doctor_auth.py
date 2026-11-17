"""B2-T1：/system/doctor 鉴权（B1 终审交接项，安全敏感）."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


def make_services() -> dict:
    # 复制自 tests/unit/test_api_core.py:26（含 admin 用户）
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
        },
    }


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    app = create_app(make_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_doctor_requires_auth(client):
    resp = await client.get("/api/v1/system/doctor")
    assert resp.status_code == 401


async def test_doctor_passes_with_admin(client):
    tok = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    resp = await client.get(
        "/api/v1/system/doctor",
        headers={"Authorization": f"Bearer {tok.json()['token']}"},
    )
    assert resp.status_code == 200
    assert "results" in resp.json()
