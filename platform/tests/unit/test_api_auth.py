"""API auth router + foundation tests (DD-13, Task 2).

Uses httpx AsyncClient with ASGITransport — no live server.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


def make_services() -> dict:
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
            "researcher": {
                "user_id": "u-res",
                "role": "researcher",
                "password_hash": auth.hash_password("res-pw"),
            },
        },
    }


@pytest.fixture
def services() -> dict:
    return make_services()


@pytest_asyncio.fixture
async def client(services):
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def login(client: AsyncClient, username: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def admin_headers(client: AsyncClient) -> dict:
    token = await login(client, "admin", "admin-pw")
    return {"Authorization": f"Bearer {token}"}


class TestLogin:
    async def test_login_success(self, client):
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"]
        assert body["expires_at"] > 0
        assert body["role"] == "admin"

    async def test_login_wrong_password(self, client):
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "nope"}
        )
        assert resp.status_code == 401
        err = resp.json()
        assert err["code"] == "UNAUTHORIZED"

    async def test_login_unknown_user(self, client):
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "x"}
        )
        assert resp.status_code == 401


class TestMe:
    async def test_me_requires_token(self, client):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_me_invalid_token(self, client):
        resp = await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer garbage"}
        )
        assert resp.status_code == 401

    async def test_me_roundtrip(self, client):
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "u-admin"
        assert body["role"] == "admin"


class TestErrorEnvelope:
    async def test_error_response_shape(self, client):
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "bad"}
        )
        body = resp.json()
        assert set(body) == {"code", "message", "detail"}
        assert body["code"] == "UNAUTHORIZED"


class TestRbac:
    async def test_researcher_cannot_create_sut(self, client):
        token = await login(client, "researcher", "res-pw")
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-1", "carrier": "linux", "image_ref": "img:1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    async def test_list_requires_auth(self, client):
        resp = await client.get("/api/v1/suts")
        assert resp.status_code == 401

    async def test_audit_entries_recorded(self, client, services):
        headers = await admin_headers(client)
        await client.post(
            "/api/v1/suts",
            json={"name": "sut-1", "carrier": "linux", "image_ref": "img:1"},
            headers=headers,
        )
        entries = services["audit_log"].entries
        assert any(e.action == "sut.create" for e in entries)
        create_entry = next(e for e in entries if e.action == "sut.create")
        assert create_entry.actor == "u-admin"
        assert create_entry.object_type == "sut"
