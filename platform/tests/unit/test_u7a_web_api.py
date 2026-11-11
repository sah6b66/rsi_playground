"""U7a backend: full settings exposure, iteration label, evalset env_parsed."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio

_ENV_YAML = """
name: env-u7
backend: fake
devices:
  - {name: R1, type: vrp, version: "8.180", config: "configs/R1.cfg"}
  - {name: R2, type: ce, config: "configs/R2.cfg"}
topology:
  links: []
ssh_access:
  R1: {ip: 10.0.0.1, port: 2001, username: admin, password: pw}
  R2: {ip: 10.0.0.2, port: 2002, username: admin, password: pw}
"""


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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestFullSettings:
    async def test_settings_expose_full_settings_tree(
        self, tmp_path, monkeypatch
    ):
        """RSI_HOME yaml（数据库/llm 段）→ GET /system/settings 出现
        database.url / llm.* 等用户级键（C1 三层配置后为用户级子集，
        派生路径/内部键/对象页键不再展示）。"""
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        from rsi.settings import Settings

        (tmp_path / "settings.yaml").write_text(
            "database:\n  url: sqlite:///u7a.db\n"
            "llm:\n  defaults:\n    judge:\n      model: qwen-u7\n",
            encoding="utf-8",
        )
        app = create_app(make_services({"settings": Settings()}))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            h = await admin_headers(c)
            resp = await c.get("/api/v1/system/settings", headers=h)
            assert resp.status_code == 200, resp.text
            items = resp.json()["items"]
            by_path = {i["path"]: i for i in items}
            # yaml 值生效
            assert by_path["database.url"]["value"] == "sqlite:///u7a.db"
            assert by_path["llm.defaults.judge.model"]["value"] == "qwen-u7"
            # 用户级键集（C1：~40 键，派生/内部/对象页键不展示）
            assert 30 <= len(by_path) <= 45
            assert "server.port" in by_path
            assert "git.repo.path" not in by_path
            assert not any(p.startswith("sandbox.backends") for p in by_path)
            # C1：用户级所有键皆可 PUT（静态键标 pending_restart）
            assert by_path["llm.defaults.judge.model"]["mutable"] is True
            assert by_path["database.url"]["mutable"] is True
            assert by_path["judge.llm.model"]["mutable"] is True
            resp = await c.put(
                "/api/v1/system/settings/database.url",
                json={"value": "sqlite:///u7a-2.db"},
                headers=h,
            )
            assert resp.status_code == 200
            assert resp.json()["pending_restart"] is True

    async def test_settings_hot_update_still_works(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        app = create_app(make_services())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            h = await admin_headers(c)
            resp = await c.put(
                "/api/v1/system/settings/judge.llm.model",
                json={"value": "gpt-u7"},
                headers=h,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["value"] == "gpt-u7"


class TestIterationLabel:
    async def test_create_with_label_and_list_detail_timeline(self, client):
        h = await admin_headers(client)
        sut = (
            await client.post(
                "/api/v1/suts",
                json={"name": "sut-u7", "carrier": "linux",
                      "image_ref": "sut-u7:1"},
                headers=h,
            )
        ).json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut, "mode": "p1", "label": "BGP排障优化-第一轮"},
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        it_id = resp.json()["iteration_id"]

        detail = (
            await client.get(f"/api/v1/iterations/{it_id}", headers=h)
        ).json()
        assert detail["label"] == "BGP排障优化-第一轮"

        listed = (await client.get("/api/v1/iterations", headers=h)).json()
        assert listed["items"][0]["label"] == "BGP排障优化-第一轮"

        timeline = (
            await client.get(f"/api/v1/iterations/{it_id}/timeline", headers=h)
        ).json()
        assert timeline["label"] == "BGP排障优化-第一轮"

    async def test_label_length_validation(self, client):
        h = await admin_headers(client)
        sut = (
            await client.post(
                "/api/v1/suts",
                json={"name": "sut-u7b", "carrier": "linux",
                      "image_ref": "sut-u7b:1"},
                headers=h,
            )
        ).json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut, "mode": "p1", "label": "x" * 65},
            headers=h,
        )
        assert resp.status_code == 422

    async def test_label_optional(self, client):
        h = await admin_headers(client)
        sut = (
            await client.post(
                "/api/v1/suts",
                json={"name": "sut-u7c", "carrier": "linux",
                      "image_ref": "sut-u7c:1"},
                headers=h,
            )
        ).json()["id"]
        resp = await client.post(
            "/api/v1/iterations",
            json={"sut_id": sut, "mode": "p1"},
            headers=h,
        )
        assert resp.status_code == 201
        it_id = resp.json()["iteration_id"]
        detail = (
            await client.get(f"/api/v1/iterations/{it_id}", headers=h)
        ).json()
        assert detail["label"] is None


class TestEvalsetEnvParsed:
    async def test_get_evalset_returns_env_parsed_without_credentials(
        self, client
    ):
        app = client._transport.app
        app.state.stores["evalsets"]["es-u7"] = {
            "id": "es-u7",
            "name": "u7",
            "purpose": "iterate",
            "tasks": [{"id": "t-001", "prompt": "diagnose bgp"}],
            "description": "",
            "env_yaml": _ENV_YAML,
            "frozen": False,
            "version": 1,
            "created_at": "2026-11-05T00:00:00Z",
            "versions": [],
        }
        h = await admin_headers(client)
        resp = await client.get("/api/v1/evalsets/es-u7", headers=h)
        assert resp.status_code == 200, resp.text
        parsed = resp.json()["env_parsed"]
        assert parsed["backend"] == "fake"
        assert parsed["name"] == "env-u7"
        assert [d["name"] for d in parsed["devices"]] == ["R1", "R2"]
        r1 = parsed["devices"][0]
        assert r1["type"] == "vrp" and r1["version"] == "8.180"
        assert r1["ip"] == "10.0.0.1" and r1["port"] == 2001
        # 凭证字段绝不出现（username/password/password_ref）
        import json as _json

        assert "password" not in _json.dumps(parsed, ensure_ascii=False)
        assert "username" not in _json.dumps(parsed, ensure_ascii=False)

    async def test_no_env_yaml_env_parsed_none(self, client):
        app = client._transport.app
        app.state.stores["evalsets"]["es-bare"] = {
            "id": "es-bare",
            "name": "bare",
            "tasks": [],
            "description": "",
            "frozen": False,
            "version": 1,
            "created_at": "2026-11-05T00:00:00Z",
            "versions": [],
        }
        h = await admin_headers(client)
        resp = await client.get("/api/v1/evalsets/es-bare", headers=h)
        assert resp.json()["env_parsed"] is None
