"""T3 web 配套：GET /iterations 列表 + DependencyStatus.detail（T2 缺口）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.orchestrator.health import HealthMonitor, build_checkers
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


def make_services(extra: dict | None = None) -> dict:
    auth = AuthService(secret="test-secret")
    services = {
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
    services.update(extra or {})
    return services


@pytest_asyncio.fixture
async def client():
    app = create_app(make_services())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _make_sut(client, h, name: str) -> str:
    resp = await client.post(
        "/api/v1/suts",
        json={"name": name, "carrier": "linux", "image_ref": f"{name}:1"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_iteration(client, h, sut_id: str) -> str:
    resp = await client.post(
        "/api/v1/iterations",
        json={"sut_id": sut_id, "profile_id": "p", "mode": "p1"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["iteration_id"]


async def _seed_three(client, h) -> list[str]:
    """3 迭代：attributing / paused / aborted，分属 3 个 SUT。"""
    s1 = await _make_sut(client, h, "sut-a")
    s2 = await _make_sut(client, h, "sut-b")
    s3 = await _make_sut(client, h, "sut-c")
    it1 = await _make_iteration(client, h, s1)  # attributing（运行中）
    it2 = await _make_iteration(client, h, s2)
    await client.post(f"/api/v1/iterations/{it2}:pause", headers=h)  # paused
    it3 = await _make_iteration(client, h, s3)
    await client.post(f"/api/v1/iterations/{it3}:abort", headers=h)  # aborted
    return [it1, it2, it3]


class TestIterationList:
    async def test_list_returns_all_three(self, client):
        h = await admin_headers(client)
        ids = await _seed_three(client, h)
        resp = await client.get("/api/v1/iterations", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {it["id"] for it in body["items"]} == set(ids)
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        # 字段与 GET :id（IterationOut）一致
        item = body["items"][0]
        assert {"id", "sut_id", "mode", "state", "current_step",
                "budget_used", "created_at"} <= set(item)

    async def test_filter_by_state_and_sut(self, client):
        h = await admin_headers(client)
        await _seed_three(client, h)
        resp = await client.get(
            "/api/v1/iterations", params={"status": "paused"}, headers=h
        )
        assert [it["state"] for it in resp.json()["items"]] == ["paused"]
        suts = await client.get("/api/v1/suts", headers=h)
        sut_b = next(s["id"] for s in suts.json()["items"] if s["name"] == "sut-b")
        resp = await client.get(
            "/api/v1/iterations", params={"sut_id": sut_b}, headers=h
        )
        items = resp.json()["items"]
        assert len(items) == 1 and items[0]["sut_id"] == sut_b

    async def test_pagination_cursor(self, client):
        h = await admin_headers(client)
        await _seed_three(client, h)
        resp = await client.get(
            "/api/v1/iterations", params={"limit": 2}, headers=h
        )
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["has_more"] is True and body["next_cursor"]
        resp2 = await client.get(
            "/api/v1/iterations",
            params={"limit": 2, "cursor": body["next_cursor"]},
            headers=h,
        )
        body2 = resp2.json()
        assert len(body2["items"]) == 1
        assert body2["has_more"] is False
        assert {it["id"] for it in body["items"]}.isdisjoint(
            {it["id"] for it in body2["items"]}
        )

    async def test_create_with_inline_profile_and_evalset(self, client):
        """向导形态：evalset_id + 内嵌 profile（profile_id 缺省自动铸号）。"""
        h = await admin_headers(client)
        sut_id = await _make_sut(client, h, "sut-w")
        resp = await client.post(
            "/api/v1/iterations",
            json={
                "sut_id": sut_id,
                "evalset_id": "eval-1",
                "mode": "p1",
                "profile": {
                    "goals": [
                        {"metric": "accuracy", "direction": "max", "weight": 0.7},
                        {"metric": "token_cost", "direction": "min", "weight": 0.3},
                    ]
                },
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]
        got = (await client.get(f"/api/v1/iterations/{it_id}", headers=h)).json()
        assert got["evalset_id"] == "eval-1"
        assert got["profile_id"].startswith("prof-")
        assert got["profile"]["goals"][0]["metric"] == "accuracy"
        # 列表同样可见 evalset 绑定
        listed = (await client.get("/api/v1/iterations", headers=h)).json()
        assert any(
            it.get("evalset_id") == "eval-1" for it in listed["items"]
        )


class TestDependencyDetail:
    async def test_down_item_carries_detail(self):
        """platform 探测 down → /system/dependencies 项带非空 detail。"""

        class FakeProbe:
            def probe_cached(self):
                return SimpleNamespace(
                    ok=False, detail="connect timeout", latency_ms=None
                )

        services = make_services(
            {
                "platform_probes": {"pnetlab": FakeProbe()},
                "health_monitor": HealthMonitor(
                    build_checkers({"platform_probes": {"pnetlab": FakeProbe()}})
                ),
            }
        )
        app = create_app(services)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            h = await admin_headers(c)
            resp = await c.get("/api/v1/system/dependencies", headers=h)
            assert resp.status_code == 200, resp.text
            items = resp.json()["items"]
            down = next(i for i in items if i["name"] == "platform:pnetlab")
            assert down["status"] == "down"
            assert down["detail"] == "connect timeout"
