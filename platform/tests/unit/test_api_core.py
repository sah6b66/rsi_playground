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

@pytest.mark.asyncio
async def test_run_stdout_endpoint(services, monkeypatch, tmp_path):
    """F1/C1: GET /runs/{id}/stdout 返回 artifacts 里的回答全文."""
    app = create_app(services)
    app.state.stores["runs"]["run-1"] = {
        "id": "run-1", "batch_id": "b", "task_id": "t", "status": "done"}
    from types import SimpleNamespace

    app.state.services["settings"] = SimpleNamespace(
        artifacts_dir=tmp_path / "artifacts")
    from rsi.orchestrator.batch import persist_stdout

    persist_stdout("run-1", "hello stdout", tmp_path / "artifacts")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/runs/run-1/stdout", headers=headers)
        assert resp.status_code == 200
        assert "hello stdout" in resp.text

        resp = await client.get("/api/v1/runs/run-none/stdout", headers=headers)
        assert resp.status_code == 404

        resp = await client.get("/api/v1/runs/run-1/stdout", headers=headers)
        # 第二次：artifact 存在但 run 不存在 -> 404（_get_anywhere 先挡）
        resp = await client.get("/api/v1/runs/run-404/stdout", headers=headers)
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trace_endpoints_fall_back_to_collector_store(services):
    """F2/W12：事件在 collector（trace_store），stores['traces'] 为空时
    traces 端点必须回退读 collector（生产路径唯一真实事件源）。"""
    from rsi.core.enums import EventType
    from rsi.core.models import TraceEvent
    from rsi.tracing.collector import TraceCollector
    from rsi.tracing.seq import SeqAllocator
    from rsi.tracing.store import TraceStore

    collector = TraceCollector(SeqAllocator(), None)
    trace_store = TraceStore(collector)
    collector.attach_store(trace_store)
    collector.ingest([
        TraceEvent(
            run_id="run-tc", task_id="t", lab_id="l", sut_id="s",
            seq=0, ts="", event_type=EventType.TOOL_CALL,
            data={"tool": "bash", "command": "display version"},
        ),
        TraceEvent(
            run_id="run-tc", task_id="t", lab_id="l", sut_id="s",
            seq=1, ts="", event_type=EventType.TOOL_RESULT,
            data={"call_id": "c"},
        ),
    ])
    collector.finalize("run-tc")
    services["trace_store"] = trace_store
    app = create_app(services)
    app.state.stores["runs"]["run-tc"] = {
        "id": "run-tc", "batch_id": "b", "task_id": "t", "status": "done"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/runs/run-tc/traces", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 2
        assert resp.json()["items"][0]["event_type"] == "tool_call"

        resp = await client.post(
            "/api/v1/traces:stats", json={"run_id": "run-tc"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["total_events"] == 2
        assert resp.json()["by_type"].get("tool_call") == 1


@pytest.mark.asyncio
async def test_batch_iteration_linkage_and_decision_final_state(services):
    """F2/W3+W2：BatchCreate.iteration_id 关联落批记录与 DB；decision 门后
    store-only 状态机进入 FINISHED。"""
    import json as _json

    app = create_app(services)
    app.state.stores["iterations"]["iter-link"] = {
        "id": "iter-link", "label": None, "sut_id": "sut-1",
        "profile_id": "prof-1", "evalset_id": "es-1", "profile": {},
        "prep_mode": "cli", "api_evalset_name": None, "mode": "p0",
        "state": "paused", "current_step": "awaiting_decision", "round": 1,
        "experiments": [], "budget_used": {"llm_calls": 0, "tokens": 0},
        "archived": False, "paused_from": "deciding", "gates": [],
        "created_at": "2026-11-13T00:00:00Z",
    }
    app.state.stores["suts"]["sut-1"] = {
        "id": "sut-1", "name": "s1", "carrier": "opencode",
        "image_ref": "img", "description": "", "created_at": "x"}
    app.state.stores["evalsets"]["es-1"] = {
        "id": "es-1", "name": "e1", "purpose": "iterate", "tasks": [],
        "version": 1, "created_at": "x", "description": "", "env": {},
        "env_yaml": "", "frozen": False}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await admin_headers(client)
        resp = await client.post(
            "/api/v1/batches",
            json={"evalset_id": "es-1", "sut_id": "sut-1",
                  "purpose": "iterate", "iteration_id": "iter-link"},
            headers=headers)
        assert resp.status_code == 201
        body = resp.json()
        assert body.get("iteration_id") == "iter-link"
        # 内存批记录带关联（timeline/tokens 兜底依赖）
        assert app.state.stores["batches"][body["id"]]["iteration_id"] == "iter-link"

        # decision 门 → FINISHED（store-only 终态转移）
        resp = await client.post(
            "/api/v1/iterations/iter-link/decision",
            json={"decision": "iterate"}, headers=headers)
        assert resp.status_code == 204
        resp = await client.get("/api/v1/iterations/iter-link", headers=headers)
        assert resp.json()["state"] == "finished"


@pytest.mark.asyncio
async def test_dashboard_aggregates_real_runs(services):
    """F2/W1：dashboard 三端点接真实 runs 聚合（不再返回 demo 序列）。"""
    app = create_app(services)
    bstore = app.state.stores["batches"]
    rstore = app.state.stores["runs"]
    # batch-1：2/2 完成；batch-2：0/1 完成
    bstore["b1"] = {"id": "b1", "evalset_id": "e", "sut_id": "sut-a",
                    "purpose": "iterate", "status": "finalized",
                    "created_at": "2026-11-13T01:00:00Z", "progress": {}}
    bstore["b2"] = {"id": "b2", "evalset_id": "e", "sut_id": "sut-a",
                    "purpose": "iterate", "status": "finalized",
                    "created_at": "2026-11-13T02:00:00Z", "progress": {}}
    rstore["r1"] = {"id": "r1", "batch_id": "b1", "task_id": "t1", "status": "done",
                    "result": {"scores": {"task_complete": True, "root_cause_accuracy": 1.0}}}
    rstore["r2"] = {"id": "r2", "batch_id": "b1", "task_id": "t2", "status": "done",
                    "result": {"scores": {"task_complete": True, "root_cause_accuracy": 0.5}}}
    rstore["r3"] = {"id": "r3", "batch_id": "b2", "task_id": "t3", "status": "done",
                    "result": {"scores": {"task_complete": False, "root_cause_accuracy": 0.0}}}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await admin_headers(client)
        resp = await client.get("/api/v1/dashboard/metrics", headers=headers)
        assert resp.status_code == 200
        series = resp.json()["series"]
        assert [p["value"] for p in series] == [1.0, 0.0]  # 按批时间序

        resp = await client.get("/api/v1/dashboard/model-compare", headers=headers)
        items = resp.json()["items"]
        assert len(items) == 1 and items[0]["sut_id"] == "sut-a"
        assert items[0]["score"] == (1.0 + 1.0 + 0.0) / 3

        resp = await client.get("/api/v1/dashboard/defect-dist", headers=headers)
        assert resp.json() == {"a": {}, "b": {}}


@pytest.mark.asyncio
async def test_object_store_restart_survival(services, tmp_path):
    """F3/B1：API 建 SUT → 模拟重启（空 stores + 水合）→ SUT 仍在。"""
    from types import SimpleNamespace

    from rsi.api.object_store import hydrate_object_stores

    settings_stub = SimpleNamespace(home=tmp_path, model_dump=dict)
    services["settings"] = settings_stub
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = await admin_headers(client)
        sut = await make_sut(client, headers, name="survivor")
    f = tmp_path / "storage" / "object-store" / "suts" / f"{sut['id']}.json"
    assert f.is_file()

    fresh_stores = {"suts": {}, "evalsets": {}, "iterations": {}}
    hydrate_object_stores(fresh_stores, tmp_path / "storage" / "object-store")
    assert fresh_stores["suts"][sut["id"]]["name"] == "survivor"
