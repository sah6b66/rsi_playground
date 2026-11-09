"""T4 web 配套：GET /iterations/{id}/timeline + mode=p0（ADR-21）+ 预算兜底。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
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
        c._app = app  # type: ignore[attr-defined]
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _seed_iteration_with_two_rounds(client, h) -> str:
    """1 迭代 + 2 轮 batches/runs + 1 experiment，全部注入内存 stores。"""
    resp = await client.post(
        "/api/v1/suts",
        json={"name": "sut-tl", "carrier": "linux", "image_ref": "sut-tl:1"},
        headers=h,
    )
    sut_id = resp.json()["id"]
    resp = await client.post(
        "/api/v1/iterations",
        json={"sut_id": sut_id, "profile_id": "p", "mode": "p1"},
        headers=h,
    )
    it_id = resp.json()["iteration_id"]

    stores = client._app.state.stores  # type: ignore[attr-defined]
    stores["iterations"][it_id]["experiments"] = ["exp-1"]
    for n in (1, 2):
        stores["batches"][f"bat-{n}"] = {
            "id": f"bat-{n}",
            "evalset_id": "es-1",
            "sut_id": sut_id,
            "purpose": "iterate",
            "profile_id": None,
            "status": "finalized",
            "paused": False,
            "progress": {},
            "created_at": f"2026-11-05T0{n}:00:00+00:00",
            "iteration_id": it_id,
            "round": n,
        }
    stores["runs"]["run-1"] = {
        "id": "run-1",
        "batch_id": "bat-1",
        "task_id": "t-001",
        "status": "succeeded",
        "result": {"tokens": 100, "latency_ms": 1200},
        "verdict": {
            "verdict": "pass",
            "score": 1.0,
            "reasons": [],
            "dimensions": {"root_cause_match": True},
        },
    }
    stores["runs"]["run-2"] = {
        "id": "run-2",
        "batch_id": "bat-1",
        "task_id": "t-002",
        "status": "succeeded",
        "result": {"tokens": 200, "latency_ms": 1800},
        "verdict": {
            "verdict": "reject",
            "score": 0.0,
            "reasons": [],
            "dimensions": {"root_cause_match": False},
        },
    }
    stores["runs"]["run-3"] = {
        "id": "run-3",
        "batch_id": "bat-2",
        "task_id": "t-003",
        "status": "succeeded",
        "result": {"tokens": 300, "latency_ms": 900},
        "verdict": {
            "verdict": "pass",
            "score": 1.0,
            "reasons": [],
            "dimensions": {"root_cause_match": True},
        },
    }
    stores["experiments"]["exp-1"] = {
        "id": "exp-1",
        "sut_id": sut_id,
        "iteration_id": it_id,
        "versions": {
            "a": {"version": 1},
            "b": {"version": 2, "noise_band": [0.6, 0.7]},
        },
        "decision": "apply",
        "evidence": [],
        "files_changed": [{"path": "skill/bgp.md", "target": "skill"}],
    }
    return it_id


class TestTimeline:
    async def test_two_rounds_tasks_and_series(self, client):
        h = await admin_headers(client)
        it_id = await _seed_iteration_with_two_rounds(client, h)
        resp = await client.get(f"/api/v1/iterations/{it_id}/timeline", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["iteration_id"] == it_id
        # 2 轮结构
        assert [r["round"] for r in body["rounds"]] == [1, 2]
        r1 = body["rounds"][0]
        assert r1["batch_ids"] == ["bat-1"]
        assert r1["config_version_before"] == 1
        assert r1["config_version_after"] == 2
        assert r1["files_changed"] == [
            {"path": "skill/bgp.md", "target": "skill"}
        ]
        # tasks 计数：第 1 轮 2 条、第 2 轮 1 条
        assert len(r1["tasks"]) == 2
        assert len(body["rounds"][1]["tasks"]) == 1
        t1 = r1["tasks"][0]
        assert t1["run_id"] == "run-1" and t1["tokens"] == 100
        assert t1["latency_ms"] == 1200 and t1["root_cause_match"] is True
        assert r1["scores"]["accuracy"] == 0.5  # 1 pass / 2 judged
        assert r1["scores"]["noise_band"] == [0.6, 0.7]
        # summary 序列长度
        assert len(body["summary"]["accuracy_series"]) == 2
        assert body["summary"]["accuracy_series"][0] == {"round": 1, "value": 0.5}
        assert body["summary"]["tokens_series"][1] == {"round": 2, "value": 300}
        assert body["summary"]["latency_series"][0] == {"round": 1, "value": 1500}

    async def test_unknown_iteration_404(self, client):
        h = await admin_headers(client)
        resp = await client.get(
            "/api/v1/iterations/iter-nope/timeline", headers=h
        )
        assert resp.status_code == 404

    async def test_empty_iteration_honest_empty(self, client):
        """无 batch/run 数据 → rounds/summary 空数组（诚实空）。"""
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-empty", "carrier": "linux",
                  "image_ref": "sut-empty:1"},
            headers=h,
        )
        sut_id = resp.json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "p", "mode": "p1"},
            headers=h,
        )
        it_id = resp.json()["iteration_id"]
        body = (
            await client.get(f"/api/v1/iterations/{it_id}/timeline", headers=h)
        ).json()
        assert body["rounds"] == []
        assert body["summary"] == {
            "accuracy_series": [],
            "tokens_series": [],
            "latency_series": [],
        }

    async def test_get_id_budget_fallback(self, client):
        """GET :id budget_used.tokens 为 0 时，由 runs 聚合兜底（600）。"""
        h = await admin_headers(client)
        it_id = await _seed_iteration_with_two_rounds(client, h)
        got = (await client.get(f"/api/v1/iterations/{it_id}", headers=h)).json()
        assert got["budget_used"]["tokens"] == 600


class TestModeP0:
    async def test_p0_accepted(self, client):
        """ADR-21：P0 人工模式是一等模式 → 201。"""
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-p0", "carrier": "linux",
                  "image_ref": "sut-p0:1"},
            headers=h,
        )
        sut_id = resp.json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "p", "mode": "p0"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]
        got = (await client.get(f"/api/v1/iterations/{it_id}", headers=h)).json()
        assert got["mode"] == "p0"

    async def test_p2_conflict(self, client):
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-p2", "carrier": "linux",
                  "image_ref": "sut-p2:1"},
            headers=h,
        )
        sut_id = resp.json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "p", "mode": "p2"},
            headers=h,
        )
        assert resp.status_code == 409
