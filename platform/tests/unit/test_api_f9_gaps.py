"""F9 API-level gap coverage (T2-6/T2-7).

- IN-017: evalset import (zip) -> export round-trip content equality.
- IN-019: POST /iterations mode=p2 -> 409; mode=p1 -> 201; same-SUT active
  iteration -> 409.
- Untested write endpoints: PUT/DELETE /suts/{id}, PUT/DELETE /evalsets/{id},
  PUT/DELETE /profiles/{id}, DELETE /labs/{id}, GET /system/doctor.
"""

from __future__ import annotations

import io
import json
import zipfile

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
            }
        },
    }


@pytest_asyncio.fixture
async def client():
    app = create_app(make_services())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def h(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------- IN-017


class TestEvalsetImportExportRoundTrip:
    async def test_round_trip_content_equality(self, client):
        h_ = await h(client)
        env_yaml = "name: lab-rt\nbackend: fake\ndevices:\n  - R1\n"
        tasks = [
            {"id": "t-001", "prompt": "Diagnose BGP peer 10.0.0.2"},
            {"id": "t-002", "prompt": "Find the interface with errors"},
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("env.yaml", env_yaml)
            zf.writestr(
                "tasks.jsonl",
                "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks),
            )
            zf.writestr("configs/R1.cfg", "interface GE0/0/1\n")
        buf.seek(0)

        resp = await client.post(
            "/api/v1/evalsets:import",
            files={"file": ("eval-rt.zip", buf.getvalue(), "application/zip")},
            data={"name": "eval-roundtrip"},
            headers=h_,
        )
        assert resp.status_code == 201, resp.text
        es_id = resp.json()["id"]
        assert resp.json()["tasks"] == tasks

        exp = await client.post(f"/api/v1/evalsets/{es_id}:export", headers=h_)
        assert exp.status_code == 200
        assert exp.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(exp.content)) as zf:
            names = set(zf.namelist())
            assert {"env.yaml", "tasks.jsonl", "configs/R1.cfg"} <= names
            # round-trip content equality (FR3.5)
            assert zf.read("env.yaml").decode() == env_yaml
            rt_tasks = [
                json.loads(line)
                for line in zf.read("tasks.jsonl").decode().splitlines()
                if line.strip()
            ]
            assert rt_tasks == tasks
            assert zf.read("configs/R1.cfg").decode() == "interface GE0/0/1\n"

        # re-import of the exported zip keeps the content stable
        buf2 = io.BytesIO(exp.content)
        resp2 = await client.post(
            "/api/v1/evalsets:import",
            files={"file": ("eval-rt2.zip", buf2.getvalue(), "application/zip")},
            data={"name": "eval-roundtrip-2"},
            headers=h_,
        )
        assert resp2.status_code == 201
        assert resp2.json()["tasks"] == tasks


# ---------------------------------------------------------------- IN-019


class TestIterationModeGate:
    async def test_p2_409_p1_ok_same_sut_409(self, client):
        h_ = await h(client)
        sut = await client.post(
            "/api/v1/suts",
            json={"name": "sut-in019", "carrier": "linux", "image_ref": "img:1"},
            headers=h_,
        )
        assert sut.status_code == 201
        sut_id = sut.json()["id"]

        # mode=p2 -> 409 (P2 未开放)
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "prof-1", "mode": "p2"},
            headers=h_,
        )
        assert resp.status_code == 409
        assert "P2" in resp.json()["message"]

        # mode=p1 -> 201 (合法路径)
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "prof-1", "mode": "p1"},
            headers=h_,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["iteration_id"]

        # 同 SUT 第二活跃迭代 -> 409（v2.14 互斥）
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut_id, "profile_id": "prof-1", "mode": "p1"},
            headers=h_,
        )
        assert resp.status_code == 409
        assert "active iteration" in resp.json()["message"]

        # 未知 SUT -> 404
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": "sut-nope", "profile_id": "prof-1", "mode": "p1"},
            headers=h_,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------- T2-6 write endpoints


class TestUntestedWriteEndpoints:
    async def test_sut_put_delete(self, client):
        h_ = await h(client)
        sut_id = (
            await client.post(
                "/api/v1/suts",
                json={"name": "sut-putdel", "carrier": "linux", "image_ref": "i:1"},
                headers=h_,
            )
        ).json()["id"]
        resp = await client.put(
            f"/api/v1/suts/{sut_id}",
            json={"image_ref": "i:2", "description": "updated"},
            headers=h_,
        )
        assert resp.status_code == 200
        assert resp.json()["image_ref"] == "i:2"
        assert resp.json()["carrier"] == "linux"  # untouched field
        resp = await client.delete(f"/api/v1/suts/{sut_id}", headers=h_)
        assert resp.status_code == 204
        assert (await client.get(f"/api/v1/suts/{sut_id}", headers=h_)).status_code == 404

    async def test_evalset_put_delete(self, client):
        h_ = await h(client)
        es_id = (
            await client.post(
                "/api/v1/evalsets",
                json={"name": "es-putdel", "tasks": [{"id": "t1", "prompt": "p"}]},
                headers=h_,
            )
        ).json()["id"]
        resp = await client.put(
            f"/api/v1/evalsets/{es_id}",
            json={"description": "v2 desc"},
            headers=h_,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "v2 desc"
        assert resp.json()["version"] == 2
        resp = await client.delete(f"/api/v1/evalsets/{es_id}", headers=h_)
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/v1/evalsets/{es_id}", headers=h_)
        ).status_code == 404

    async def test_profile_put_delete(self, client):
        h_ = await h(client)
        prof_id = (
            await client.post(
                "/api/v1/profiles",
                json={
                    "name": "prof-putdel",
                    "evalsets": {"iterate": "es-1", "smoke": "", "holdout": ""},
                    "goals": [
                        {"metric": "task_complete", "direction": "max", "weight": 1.0}
                    ],
                    "enabled_targets": ["system_prompt"],
                },
                headers=h_,
            )
        ).json()["id"]
        resp = await client.put(
            f"/api/v1/profiles/{prof_id}",
            json={"name": "prof-putdel-v2"},
            headers=h_,
        )
        assert resp.status_code == 200
        assert resp.json()["profile"]["name"] == "prof-putdel-v2"
        resp = await client.delete(f"/api/v1/profiles/{prof_id}", headers=h_)
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/v1/profiles/{prof_id}", headers=h_)
        ).status_code == 404

    async def test_lab_delete(self, client):
        h_ = await h(client)
        lab_id = (
            await client.post(
                "/api/v1/labs",
                json={"sut_id": "sut-x", "topology": {}},
                headers=h_,
            )
        ).json()["id"]
        resp = await client.delete(f"/api/v1/labs/{lab_id}", headers=h_)
        assert resp.status_code == 204
        assert (
            await client.get(f"/api/v1/labs/{lab_id}", headers=h_)
        ).status_code == 404

    async def test_system_doctor(self, client):
        h_ = await h(client)
        resp = await client.get("/api/v1/system/doctor", headers=h_)
        assert resp.status_code == 200
        report = resp.json()
        # Doctor report shape: results[] with per-check status (DD doctor)
        results = report.get("results", [])
        assert results and all("name" in r and "status" in r for r in results)
        assert any(r["status"] == "pass" for r in results)
