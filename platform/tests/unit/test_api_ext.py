"""Extended API router tests (DD-13, Task 4: iterations/labs/instances/
judgments/experiments/traces)."""

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
def services():
    return make_services()


@pytest_asyncio.fixture
async def client(services):
    app = create_app(services)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
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


# -- iterations ---------------------------------------------------------------


class TestIterations:
    async def test_lifecycle(self, client):
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": "sut-1", "profile_id": "prof-1", "mode": "auto"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]
        assert it_id

        resp = await client.get(f"/api/v1/iterations/{it_id}", headers=h)
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "attributing"
        assert body["round"] >= 1
        assert "experiments" in body and "budget_used" in body
        assert "current_step" in body

        resp = await client.post(f"/api/v1/iterations/{it_id}:pause", headers=h)
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"

        resp = await client.post(f"/api/v1/iterations/{it_id}:resume", headers=h)
        assert resp.status_code == 200
        assert resp.json()["state"] == "attributing"

        resp = await client.post(f"/api/v1/iterations/{it_id}:abort", headers=h)
        assert resp.status_code == 200
        assert resp.json()["state"] == "aborted"

    async def test_pause_requires_auth(self, client):
        resp = await client.post("/api/v1/iterations/x:pause")
        assert resp.status_code in (401, 403)

    async def test_missing_iteration_404(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/iterations/nope", headers=h)
        assert resp.status_code == 404

    async def test_abort_on_aborted_409(self, client):
        h = await admin_headers(client)
        it = (
            await client.post(
                "/api/v1/iterations",
                json={"sut_id": "s", "profile_id": "p", "mode": "auto"},
                headers=h,
            )
        ).json()["iteration_id"]
        await client.post(f"/api/v1/iterations/{it}:abort", headers=h)
        resp = await client.post(f"/api/v1/iterations/{it}:abort", headers=h)
        assert resp.status_code == 409

    async def test_delete_archives(self, client):
        h = await admin_headers(client)
        it = (
            await client.post(
                "/api/v1/iterations",
                json={"sut_id": "s", "profile_id": "p", "mode": "auto"},
                headers=h,
            )
        ).json()["iteration_id"]
        resp = await client.delete(f"/api/v1/iterations/{it}", headers=h)
        assert resp.status_code == 204
        resp = await client.get(f"/api/v1/iterations/{it}", headers=h)
        assert resp.status_code == 200
        assert resp.json()["archived"] is True

    async def test_four_human_gates(self, client):
        h = await admin_headers(client)
        it = (
            await client.post(
                "/api/v1/iterations",
                json={"sut_id": "s", "profile_id": "p", "mode": "auto"},
                headers=h,
            )
        ).json()["iteration_id"]

        gates = [
            ("confirm-direction", {"selected": "target-A"}),
            ("hypothesis", {"hypothesis": "reduce tool timeouts"}),
            ("review", {"approved": True}),
            ("decision", {"decision": "promote"}),
        ]
        for name, body in gates:
            await client.post(f"/api/v1/iterations/{it}:pause", headers=h)
            resp = await client.post(
                f"/api/v1/iterations/{it}/{name}", json=body, headers=h
            )
            assert resp.status_code == 204, (name, resp.text)
            state = (
                await client.get(f"/api/v1/iterations/{it}", headers=h)
            ).json()["state"]
            assert state != "paused"

    async def test_gate_on_running_409(self, client):
        h = await admin_headers(client)
        it = (
            await client.post(
                "/api/v1/iterations",
                json={"sut_id": "s", "profile_id": "p", "mode": "auto"},
                headers=h,
            )
        ).json()["iteration_id"]
        resp = await client.post(
            f"/api/v1/iterations/{it}/decision", json={"decision": "promote"},
            headers=h,
        )
        assert resp.status_code == 409


# -- labs ---------------------------------------------------------------------


class TestLabs:
    async def test_list_create_topology_delete(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/labs", headers=h)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

        resp = await client.post(
            "/api/v1/labs",
            json={"sut_id": "sut-1", "topology": {"name": "t1"}},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        lab = resp.json()
        assert lab["id"]
        assert lab["online"] is True

        resp = await client.get("/api/v1/labs", headers=h)
        assert len(resp.json()["items"]) == 1
        assert resp.json()["items"][0]["online"] is True

        resp = await client.get(f"/api/v1/labs/{lab['id']}/topology", headers=h)
        assert resp.status_code == 200
        topo = resp.json()
        assert "nodes" in topo and "links" in topo

        resp = await client.post(f"/api/v1/labs/{lab['id']}/reset", headers=h)
        assert resp.status_code == 200

        resp = await client.delete(f"/api/v1/labs/{lab['id']}", headers=h)
        assert resp.status_code == 204
        resp = await client.get(f"/api/v1/labs/{lab['id']}", headers=h)
        assert resp.status_code == 404

    async def test_backends(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/sandbox/backends", headers=h)
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)


# -- instances ----------------------------------------------------------------


class TestInstances:
    async def test_list_delete(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/instances", headers=h)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

        # seed via internal store is awkward; create through labs? Use API-less
        # path: instances appear only from runs in P0 — check list shape only.
        resp = await client.delete("/api/v1/instances/i-1", headers=h)
        assert resp.status_code == 404


# -- judgments ----------------------------------------------------------------


class TestJudgments:
    async def test_get_by_run(self, client, services):
        services["stores"] = None  # use default
        h = await res_headers(client)
        run = {"id": "run-1", "batch_id": "b", "status": "done",
               "verdict": {"score": 0.9, "verdict": "pass"}}
        client._transport.app.state.stores["runs"]["run-1"] = run
        resp = await client.get("/api/v1/runs/run-1/judgment", headers=h)
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.9

    async def test_pairwise(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["runs"]["ra"] = {"id": "ra", "batch_id": "b", "status": "done",
                                "verdict": {"score": 0.8, "verdict": "pass"}}
        stores["runs"]["rb"] = {"id": "rb", "batch_id": "b", "status": "done",
                                "verdict": {"score": 0.5, "verdict": "fail"}}
        resp = await client.post(
            "/api/v1/judgments:pairwise",
            json={"run_a": "ra", "run_b": "rb"},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["winner"] == "ra"

    async def test_judgment_404(self, client):
        h = await res_headers(client)
        resp = await client.get("/api/v1/runs/zz/judgment", headers=h)
        assert resp.status_code == 404


# -- experiments --------------------------------------------------------------


class TestExperiments:
    async def test_list_detail_compare(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["experiments"]["exp-1"] = {
            "id": "exp-1",
            "sut_id": "sut-1",
            "iteration_id": "it-1",
            "versions": {
                "a": {"version": "v1", "score": 0.6},
                "b": {"version": "v2", "score": 0.7},
            },
            "decision": "promoted",
            "evidence": [{"kind": "holdout", "delta": 0.1}],
        }
        resp = await client.get("/api/v1/experiments?sut_id=sut-1", headers=h)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

        resp = await client.get("/api/v1/experiments/exp-1", headers=h)
        assert resp.status_code == 200
        assert resp.json()["decision"] == "promoted"
        assert resp.json()["evidence"]

        resp = await client.get("/api/v1/experiments/exp-1/compare", headers=h)
        assert resp.status_code == 200
        cmp_ = resp.json()
        assert cmp_["a"]["version"] == "v1"
        assert cmp_["b"]["version"] == "v2"
        assert "diff" in cmp_


# -- traces -------------------------------------------------------------------


class TestTraces:
    async def test_diff(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["runs"]["ra"] = {"id": "ra", "batch_id": "b", "status": "done"}
        stores["runs"]["rb"] = {"id": "rb", "batch_id": "b", "status": "done"}
        stores["traces"]["ra"] = [
            {"seq": 1, "type": "llm_request"},
            {"seq": 2, "type": "tool_call"},
        ]
        stores["traces"]["rb"] = [
            {"seq": 1, "type": "llm_request"},
            {"seq": 2, "type": "error"},
        ]
        resp = await client.get(
            "/api/v1/traces/diff?run_a=ra&run_b=rb", headers=h
        )
        assert resp.status_code == 200
        diff = resp.json()
        assert diff["run_a"] == "ra" and diff["run_b"] == "rb"
        assert "by_type" in diff
        assert diff["by_type"]["llm_request"] == [1, 1]

    async def test_query_with_filter(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["runs"]["ra"] = {"id": "ra", "batch_id": "b", "status": "done"}
        stores["traces"]["ra"] = [
            {"seq": 1, "type": "llm_request", "run_id": "ra"},
            {"seq": 2, "type": "tool_call", "run_id": "ra"},
            {"seq": 3, "type": "tool_call", "run_id": "ra"},
        ]
        resp = await client.post(
            "/api/v1/traces:query",
            json={"run_id": "ra", "type": "tool_call"},
            headers=h,
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        assert all(e["type"] == "tool_call" for e in items)

    async def test_stats(self, client):
        h = await res_headers(client)
        stores = client._transport.app.state.stores
        stores["runs"]["ra"] = {"id": "ra", "batch_id": "b", "status": "done"}
        stores["traces"]["ra"] = [
            {"seq": 1, "type": "llm_request"},
            {"seq": 2, "type": "tool_call"},
            {"seq": 3, "type": "tool_call"},
        ]
        resp = await client.post(
            "/api/v1/traces:stats", json={"run_id": "ra"}, headers=h
        )
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_events"] == 3
        assert stats["by_type"]["tool_call"] == 2
