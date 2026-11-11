"""System/dashboard/audit/instrumentation + SSE/WS tests (DD-13, Task 5)."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
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
def services():
    return make_services()


@pytest_asyncio.fixture
async def client(services):
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def res_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "researcher", "password": "res-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestSystem:
    async def test_health_public(self, client):
        resp = await client.get("/api/v1/system/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_dependencies(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/system/dependencies", headers=h)
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

    async def test_settings_require_admin(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/system/settings", headers=h)
        assert resp.status_code == 403

    async def test_settings_admin_ok(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/system/settings", headers=h)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items and all("path" in i for i in items)
        # 参数页可见性：评估集在线 API / 网络准备 API 配置段必须在列
        paths = {i["path"] for i in items}
        assert "evalset_api.url" in paths
        assert "network_prep_api.url" in paths
        assert "network_prep_api.timeout_s" in paths

    async def test_settings_update(self, client):
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/system/settings/judge.llm.model",
            json={"value": "gpt-x"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"] == "gpt-x"

    async def test_settings_update_static_409(self, client):
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/system/settings/platform.static_item",
            json={"value": 1},
            headers=h,
        )
        assert resp.status_code == 409

    async def test_stop_toggle(self, client):
        h = await admin_headers(client)
        resp = await client.post("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
        assert resp.json()["stopped"] is True
        resp = await client.delete("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
        assert resp.json()["stopped"] is False

    async def test_stop_requires_admin(self, client):
        h = await res_headers(client)
        resp = await client.post("/api/v1/system:stop", headers=h)
        assert resp.status_code == 403

    async def test_freeze(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/system/freeze", headers=h)
        assert resp.status_code == 200
        assert "frozen" in resp.json()

    async def test_logs(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/system/logs", headers=h)
        assert resp.status_code == 200
        assert "items" in resp.json()


class TestDashboard:
    async def test_metrics_series(self, client):
        h = await res_headers(client)
        resp = await client.get(
            "/api/v1/dashboard/metrics?metric=success_rate", headers=h
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["metric"] == "success_rate"
        assert isinstance(body["series"], list)
        assert len(body["series"]) > 0

    async def test_model_compare(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/dashboard/model-compare", headers=h)
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

    async def test_defect_dist(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/dashboard/defect-dist", headers=h)
        assert resp.status_code == 200
        assert "a" in resp.json() and "b" in resp.json()


class TestAuditLogs:
    async def test_query(self, client, services):
        services["audit_log"].record(
            actor="admin", action="test.action",
            object_type="thing", object_id="t-1",
        )
        h = await admin_headers(client)
        resp = await client.get("/api/v1/audit-logs", headers=h)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(e["action"] == "test.action" for e in items)

    async def test_requires_admin(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/audit-logs", headers=h)
        assert resp.status_code == 403


class TestInstrumentation:
    async def test_coverage(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["batches"]["b-1"] = {
            "id": "b-1", "evalset_id": "e", "sut_id": "s",
            "purpose": "iterate", "profile_id": None, "status": "finalized",
            "paused": False, "progress": {"done": 1, "total": 1},
            "created_at": "2026-01-01T00:00:00Z",
        }
        resp = await client.get("/api/v1/batches/b-1/coverage", headers=h)
        assert resp.status_code == 200
        cov = resp.json()
        assert cov["batch_id"] == "b-1"
        # B-13: DD-06 CoverageReport shape
        assert "covered" in cov and "not_instrumentable" in cov

    async def test_coverage_404(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/batches/zz/coverage", headers=h)
        assert resp.status_code == 404


class TestSSE:
    async def test_stream_initial_event(self, client):
        h = await res_headers(client)
        # bound stream lifetime so the in-memory transport can finish
        client._transport.app.state.stores["settings"]["sse.max_seconds"] = {
            "value": 0.5, "mutable": True,
        }
        async with client.stream(
            "GET", "/api/v1/streams/events", headers=h
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    assert payload["event"] == "stream.open"
                    break


def _ws_token(services) -> str:
    auth = services["auth_service"]
    rec = services["users"]["admin"]
    token, _ = auth.login(rec["user_id"], rec["role"], rec["password_hash"], "admin-pw")
    return token


class TestWS:
    def test_ws_tail(self, services):
        app = create_app(services)
        app.state.stores["runs"]["run-1"] = {
            "id": "run-1", "batch_id": "b", "status": "done",
        }
        app.state.stores["traces"]["run-1"] = [
            {"seq": i, "type": "tool_call", "run_id": "run-1"} for i in range(5)
        ]
        with TestClient(app) as tc:
            with tc.websocket_connect(
                f"/api/v1/streams/runs/run-1?token={_ws_token(services)}"
            ) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "tail"
                assert len(msg["events"]) == 5

    def test_ws_missing_run_closes(self, services):
        app = create_app(services)
        with TestClient(app) as tc:
            with tc.websocket_connect(
                f"/api/v1/streams/runs/zz?token={_ws_token(services)}"
            ) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error"

    def test_ws_append_incremental_and_heartbeat(self, services):
        # N-5: after the tail frame the socket stays open, pushes
        # {"type":"append"} frames for new trace events and sends
        # {"type":"ping"} heartbeats when idle (short interval injected
        # via the settings store for testability).
        app = create_app(services)
        app.state.stores["runs"]["run-1"] = {
            "id": "run-1", "batch_id": "b", "status": "running",
        }
        app.state.stores["traces"]["run-1"] = [
            {"seq": 0, "type": "tool_call", "run_id": "run-1"},
        ]
        app.state.stores["settings"]["ws.heartbeat_s"] = {
            "value": 0.1, "mutable": True,
        }
        with TestClient(app) as tc:
            with tc.websocket_connect(
                f"/api/v1/streams/runs/run-1?token={_ws_token(services)}"
            ) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "tail"
                # new trace event appears -> append frame on the next tick
                app.state.stores["traces"]["run-1"].append(
                    {"seq": 1, "type": "tool_call", "run_id": "run-1"}
                )
                m2 = ws.receive_json()
                if m2["type"] == "ping":  # tolerate a leading heartbeat
                    m2 = ws.receive_json()
                assert m2["type"] == "append"
                assert [e["seq"] for e in m2["events"]] == [1]
                assert m2["cursor"] == 2
                # idle -> heartbeat ping frames arrive within a few ticks
                got_ping = False
                for _ in range(30):
                    m3 = ws.receive_json()
                    if m3["type"] == "ping" and "ts" in m3:
                        got_ping = True
                        break
                assert got_ping, "no heartbeat ping received while idle"
            # after the client disconnects the subscription is cleaned up
            bus = app.state.event_bus
            assert len(bus._subs) == 0


class TestOpenAPI:
    async def test_openapi_customized(self, client):
        # N-6: servers / tags / Error schema customization is live.
        resp = await client.get("/api/v1/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert any(str(s.get("url", "")).endswith("/api/v1")
                   for s in schema.get("servers", []))
        tags = {t["name"]: t.get("description", "") for t in schema["tags"]}
        assert tags.get("auth"), "tag descriptions missing"
        assert len(tags) >= 15
        err = schema["components"]["schemas"]["Error"]
        assert "code" in err["properties"]
        assert "message" in err["properties"]
