"""Full-workflow API integration test (Plan 4, Task 6).

Exercises the complete user journey through the REST API:
login -> create SUT/evalset/profile -> batch -> runs -> judgment ->
dashboard -> system -> audit -> iteration -> labs.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


class FakeBatchRunner:
    def __init__(self):
        self.triggered: list[dict] = []

    def trigger(self, batch: dict) -> None:
        self.triggered.append(batch)
        batch["status"] = "running"
        batch["progress"] = {"done": 0, "total": 2}


def make_services() -> dict:
    auth = AuthService(secret="test-secret")
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "batch_runner": FakeBatchRunner(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
        },
    }


@pytest.fixture
def services():
    return make_services()


@pytest_asyncio.fixture
async def app(services):
    return create_app(services)


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestFullApiWorkflow:
    async def test_full_api_workflow(self, client, app, services):
        # 1-2. Login and verify identity
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin-pw"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        h = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/v1/auth/me", headers=h)
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "u-admin"
        assert resp.json()["role"] == "admin"

        # 3-4. Create a SUT and verify it appears in the list
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-main", "carrier": "linux", "image_ref": "img:1"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        sut = resp.json()
        assert sut["id"]

        resp = await client.get("/api/v1/suts", headers=h)
        assert resp.status_code == 200
        assert any(s["id"] == sut["id"] for s in resp.json()["items"])

        # 5-6. Create an evalset and freeze it
        resp = await client.post(
            "/api/v1/evalsets",
            json={
                "name": "es-main",
                "tasks": [{"id": "t-1", "prompt": "do it"}],
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        es = resp.json()
        assert es["frozen"] is False

        resp = await client.post(f"/api/v1/evalsets/{es['id']}:freeze", headers=h)
        assert resp.status_code == 200
        assert resp.json()["frozen"] is True

        # 7. Create an optimization profile
        resp = await client.post(
            "/api/v1/profiles",
            json={
                "name": "profile-main",
                "evalsets": {
                    "iterate": es["id"],
                    "holdout": es["id"],
                    "smoke": es["id"],
                },
                "goals": [{"metric": "success_rate", "direction": "max", "weight": 1.0}],
                "enabled_targets": ["system_prompt"],
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        profile = resp.json()

        # 8-9. Trigger a batch and check its status
        resp = await client.post(
            "/api/v1/batches",
            json={
                "evalset_id": es["id"],
                "sut_id": sut["id"],
                "purpose": "iterate",
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        batch = resp.json()
        assert batch["status"] == "running"
        assert services["batch_runner"].triggered

        resp = await client.get(f"/api/v1/batches/{batch['id']}", headers=h)
        assert resp.status_code == 200
        assert resp.json()["id"] == batch["id"]
        assert "progress" in resp.json()

        resp = await client.get(f"/api/v1/batches/{batch['id']}/runs", headers=h)
        assert resp.status_code == 200
        assert "items" in resp.json()

        # 10-11. Seed a completed run under this batch, then read run + verdict
        app.state.stores["runs"]["run-1"] = {
            "id": "run-1",
            "batch_id": batch["id"],
            "task_id": "fault/demo-01",
            "status": "done",
            "verdict": {"score": 0.92, "verdict": "pass"},
        }
        resp = await client.get("/api/v1/runs/run-1", headers=h)
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"

        resp = await client.get("/api/v1/runs/run-1/judgment", headers=h)
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.92

        # 12. Dashboard metrics respond
        resp = await client.get(
            "/api/v1/dashboard/metrics?metric=success_rate", headers=h
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["metric"] == "success_rate"
        assert isinstance(body["series"], list) and body["series"]

        # 13-14. System health and dependencies
        resp = await client.get("/api/v1/system/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        resp = await client.get("/api/v1/system/dependencies", headers=h)
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

        # 15. Audit trail has entries from the actions above
        resp = await client.get("/api/v1/audit-logs", headers=h)
        assert resp.status_code == 200
        actions = {e["action"] for e in resp.json()["items"]}
        assert "sut.create" in actions
        assert "evalset.freeze" in actions or "evalset.create" in actions
        assert "batch.trigger" in actions or "batch.create" in actions

        # 16-17. Create an iteration and verify its state
        resp = await client.post(
            "/api/v1/iterations",
            json={
                "sut_id": sut["id"],
                "profile_id": profile["id"],
                "mode": "p1",
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]

        resp = await client.get(f"/api/v1/iterations/{it_id}", headers=h)
        assert resp.status_code == 200
        it = resp.json()
        assert it["state"] == "attributing"
        assert it["round"] >= 1
        assert "experiments" in it and "budget_used" in it

        # 18. Labs list responds
        resp = await client.get("/api/v1/labs", headers=h)
        assert resp.status_code == 200
        assert resp.json()["items"] == []
