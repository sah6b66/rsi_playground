"""API core business routers tests (DD-13, Task 3)."""

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
    runner = FakeBatchRunner()
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "batch_runner": runner,
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
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def researcher_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "researcher", "password": "res-pw"}
    )
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


async def make_sut(client, headers, name="sut-1") -> dict:
    resp = await client.post(
        "/api/v1/suts",
        json={"name": name, "carrier": "linux", "image_ref": "img:1"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


PROFILE_BODY = {
    "name": "profile-1",
    "evalsets": {"iterate": "es-1", "holdout": "es-2", "smoke": "es-3"},
    "goals": [{"metric": "success_rate", "direction": "max", "weight": 1.0}],
    "enabled_targets": ["system_prompt"],
}


class TestSuts:
    async def test_crud_roundtrip(self, client):
        headers = await admin_headers(client)
        created = await make_sut(client, headers)
        assert created["name"] == "sut-1"
        assert created["id"]

        resp = await client.get(f"/api/v1/suts/{created['id']}", headers=headers)
        assert resp.status_code == 200

        resp = await client.put(
            f"/api/v1/suts/{created['id']}",
            json={"description": "updated"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "updated"
        assert resp.json()["name"] == "sut-1"  # unchanged fields preserved

        resp = await client.delete(
            f"/api/v1/suts/{created['id']}", headers=headers
        )
        assert resp.status_code == 204
        resp = await client.get(f"/api/v1/suts/{created['id']}", headers=headers)
        assert resp.status_code == 404

    async def test_list_pagination(self, client):
        headers = await admin_headers(client)
        for i in range(5):
            await make_sut(client, headers, name=f"sut-{i}")
        resp = await client.get("/api/v1/suts?limit=2", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        page = resp.headers["x-pagination"]
        assert '"has_more": true' in page

        resp2 = await client.get(
            "/api/v1/suts?limit=2&cursor=" + resp.json()["next_cursor"],
            headers=headers,
        )
        assert len(resp2.json()["items"]) == 2
        ids1 = {s["id"] for s in resp.json()["items"]}
        ids2 = {s["id"] for s in resp2.json()["items"]}
        assert not ids1 & ids2

    async def test_researcher_cannot_create(self, client):
        headers = await researcher_headers(client)
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-x", "carrier": "linux", "image_ref": "img:1"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_missing_returns_404_envelope(self, client):
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/suts/nope", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"


class TestEvalsets:
    async def test_crud_and_freeze(self, client):
        headers = await admin_headers(client)
        resp = await client.post(
            "/api/v1/evalsets",
            json={"name": "es-1", "tasks": []},
            headers=headers,
        )
        assert resp.status_code == 201
        es = resp.json()
        assert es["frozen"] is False

        resp = await client.post(f"/api/v1/evalsets/{es['id']}:freeze", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["frozen"] is True

        # second freeze -> conflict
        resp = await client.post(f"/api/v1/evalsets/{es['id']}:freeze", headers=headers)
        assert resp.status_code == 409

        # frozen evalset cannot be mutated
        resp = await client.put(
            f"/api/v1/evalsets/{es['id']}", json={"name": "es-1b"}, headers=headers
        )
        assert resp.status_code == 409

        resp = await client.get(f"/api/v1/evalsets/{es['id']}/versions", headers=headers)
        assert resp.status_code == 200
        versions = resp.json()["items"]
        assert len(versions) == 1
        assert versions[0]["frozen"] is True

    async def test_delete_missing_404(self, client):
        headers = await admin_headers(client)
        resp = await client.delete("/api/v1/evalsets/none", headers=headers)
        assert resp.status_code == 404


class TestProfiles:
    async def test_crud(self, client):
        headers = await admin_headers(client)
        resp = await client.post("/api/v1/profiles", json=PROFILE_BODY, headers=headers)
        assert resp.status_code == 201, resp.text
        pid = resp.json()["id"]

        resp = await client.get(f"/api/v1/profiles/{pid}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == "profile-1"

        resp = await client.get("/api/v1/profiles", headers=headers)
        assert len(resp.json()["items"]) == 1

        resp = await client.put(
            f"/api/v1/profiles/{pid}", json={"name": "profile-2"}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == "profile-2"

        resp = await client.delete(f"/api/v1/profiles/{pid}", headers=headers)
        assert resp.status_code == 204


class TestBatches:
    async def test_trigger_and_status(self, client, services):
        headers = await admin_headers(client)
        sut = await make_sut(client, headers)
        es = (
            await client.post(
                "/api/v1/evalsets", json={"name": "es-1", "tasks": []}, headers=headers
            )
        ).json()

        resp = await client.post(
            "/api/v1/batches",
            json={
                "evalset_id": es["id"],
                "sut_id": sut["id"],
                "purpose": "iterate",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        batch = resp.json()
        assert batch["status"] == "running"  # runner mock flipped it
        assert services["batch_runner"].triggered

        resp = await client.get(f"/api/v1/batches/{batch['id']}", headers=headers)
        assert resp.status_code == 200
        assert "progress" in resp.json()

    async def test_trigger_unknown_refs_404(self, client):
        headers = await admin_headers(client)
        resp = await client.post(
            "/api/v1/batches",
            json={"evalset_id": "none", "sut_id": "none", "purpose": "iterate"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_pause_resume_abort(self, client):
        headers = await admin_headers(client)
        sut = await make_sut(client, headers)
        es = (
            await client.post(
                "/api/v1/evalsets", json={"name": "es-1", "tasks": []}, headers=headers
            )
        ).json()
        batch = (
            await client.post(
                "/api/v1/batches",
                json={"evalset_id": es["id"], "sut_id": sut["id"], "purpose": "iterate"},
                headers=headers,
            )
        ).json()

        resp = await client.post(f"/api/v1/batches/{batch['id']}:pause", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["paused"] is True

        resp = await client.post(f"/api/v1/batches/{batch['id']}:resume", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["paused"] is False

        resp = await client.post(f"/api/v1/batches/{batch['id']}:abort", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "aborted"

        # aborted batch is terminal: abort again -> 409
        resp = await client.post(f"/api/v1/batches/{batch['id']}:abort", headers=headers)
        assert resp.status_code == 409

    async def test_list_runs_in_batch(self, client):
        headers = await admin_headers(client)
        sut = await make_sut(client, headers)
        es = (
            await client.post(
                "/api/v1/evalsets", json={"name": "es-1", "tasks": []}, headers=headers
            )
        ).json()
        batch = (
            await client.post(
                "/api/v1/batches",
                json={"evalset_id": es["id"], "sut_id": sut["id"], "purpose": "iterate"},
                headers=headers,
            )
        ).json()
        resp = await client.get(f"/api/v1/batches/{batch['id']}/runs", headers=headers)
        assert resp.status_code == 200
        assert "items" in resp.json()


class TestRunsSeeded:
    """Runs tested against a pre-seeded store."""

    @pytest_asyncio.fixture
    async def seeded_client(self, services):
        app = create_app(services)
        app.state.stores["runs"]["run-1"] = {
            "id": "run-1",
            "batch_id": "batch-1",
            "task_id": "fault/demo-01",
            "status": "done",
        }
        app.state.stores["traces"]["run-1"] = [
            {"seq": i, "event_type": "tool_call", "data": {}} for i in range(5)
        ]
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c, services

    async def test_run_detail(self, seeded_client):
        client, _ = seeded_client
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/runs/run-1", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == "run-1"
        assert resp.json()["status"] == "done"

    async def test_run_404(self, seeded_client):
        client, _ = seeded_client
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/runs/none", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    async def test_traces_paginated(self, seeded_client):
        client, _ = seeded_client
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/runs/run-1/traces?limit=3", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3
        assert resp.json()["next_cursor"]

    async def test_rejudge(self, seeded_client):
        client, services = seeded_client
        headers = await admin_headers(client)
        resp = await client.post("/api/v1/runs/run-1:re-judge", headers=headers)
        assert resp.status_code == 202
        assert resp.json()["status"] == "judging"
        assert any(e.action == "run.rejudge" for e in services["audit_log"].entries)
