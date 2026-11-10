"""H1 (final review) config-chain fixes:

A — Settings reads settings.yaml (env > yaml > defaults) via the built-in
     YamlConfigSettingsSource;
B — PUT /system/settings/{path} updates the *shared* Settings object;
C — the PUT persists the leaf key to settings.yaml (restart-effective);
plus settings store derived from the real Settings instance and the
admin reset-password endpoint.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.settings import Settings, settings_yaml_path

pytestmark = pytest.mark.asyncio


def _clean_env(monkeypatch, keep_home: bool = True):
    home = os.environ.get("RSI_HOME")
    for key in list(os.environ):
        if key.startswith("RSI_"):
            monkeypatch.delenv(key, raising=False)
    if keep_home and home:
        monkeypatch.setenv("RSI_HOME", home)


# ── A: yaml config source ────────────────────────────────────────────────


async def test_yaml_overrides_default(monkeypatch, tmp_path):
    _clean_env(monkeypatch, keep_home=False)
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"sandbox": {"build_retry": 5}}), encoding="utf-8"
    )
    assert Settings().sandbox.build_retry == 5


async def test_env_overrides_yaml(monkeypatch, tmp_path):
    _clean_env(monkeypatch, keep_home=False)
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"sandbox": {"build_retry": 5}}), encoding="utf-8"
    )
    monkeypatch.setenv("RSI_SANDBOX__BUILD_RETRY", "9")
    assert Settings().sandbox.build_retry == 9


async def test_missing_yaml_defaults(monkeypatch, tmp_path):
    _clean_env(monkeypatch, keep_home=False)
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    s = Settings()
    assert s.sandbox.build_retry == 2
    assert s.server.port == 8000


async def test_yaml_nested_section(monkeypatch, tmp_path):
    _clean_env(monkeypatch, keep_home=False)
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "sandbox": {"build_retry": 5},
                "judge": {"det_timeout_s": 77},
                "logging": {"level": "debug"},
            }
        ),
        encoding="utf-8",
    )
    s = Settings()
    assert s.sandbox.build_retry == 5
    assert s.judge.det_timeout_s == 77
    assert s.logging.level == "debug"


# ── B + C: live PUT touches the shared Settings and writes yaml ─────────


def make_services(tmp_path, monkeypatch) -> dict:
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    for key in list(os.environ):
        if key.startswith("RSI_") and key not in ("RSI_HOME",):
            monkeypatch.delenv(key, raising=False)
    auth = AuthService(secret="test-secret")
    settings = Settings()
    return {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "settings": settings,
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
            "svc": {
                "user_id": "u-svc",
                "role": "service",
                "password_hash": auth.hash_password("svc-pw"),
            },
        },
    }


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    services = make_services(tmp_path, monkeypatch)
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c._services = services  # type: ignore[attr-defined]
        yield c


async def _admin(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_put_updates_shared_settings_and_yaml(tmp_path, client):
    h = await _admin(client)
    resp = await client.put(
        "/api/v1/system/settings/sandbox.build_retry",
        json={"value": "7"},  # str → converted via field annotation
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["value"] == 7
    assert body["source"] == "yaml+runtime"
    # ① shared Settings object updated in place
    assert client._services["settings"].sandbox.build_retry == 7
    # ② yaml file contains the leaf key
    doc = yaml.safe_load(
        settings_yaml_path(tmp_path).read_text(encoding="utf-8")
    )
    assert doc["sandbox"]["build_retry"] == 7
    # ③ a fresh Settings() (restart simulation) reads the yaml value
    assert Settings().sandbox.build_retry == 7


async def test_put_invalid_value_422(client):
    h = await _admin(client)
    resp = await client.put(
        "/api/v1/system/settings/judge.llm_retry",
        json={"value": "not-a-number"},
        headers=h,
    )
    assert resp.status_code == 422


async def test_put_static_409(client):
    h = await _admin(client)
    resp = await client.put(
        "/api/v1/system/settings/platform.static_item",
        json={"value": 1},
        headers=h,
    )
    assert resp.status_code == 409


# ── settings store derived from the real Settings instance ───────────────


async def test_get_settings_reflects_yaml(tmp_path, monkeypatch):
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"sandbox": {"build_retry": 4}}), encoding="utf-8"
    )
    services = make_services(tmp_path, monkeypatch)
    services["settings"] = Settings()
    app = create_app(services)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        h = await _admin(c)
        resp = await c.get("/api/v1/system/settings", headers=h)
        assert resp.status_code == 200
        items = {i["path"]: i for i in resp.json()["items"]}
        assert items["sandbox.build_retry"]["value"] == 4


# ── reset-password ────────────────────────────────────────────────────────


async def test_reset_password_flow(tmp_path, client):
    h = await _admin(client)
    resp = await client.post(
        "/api/v1/users",
        json={"username": "eng-9", "password": "first-pw-1", "role": "researcher"},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    uid = resp.json()["id"]

    async def login(password: str) -> int:
        r = await client.post(
            "/api/v1/auth/login", json={"username": "eng-9", "password": password}
        )
        return r.status_code

    assert await login("first-pw-1") == 200
    resp = await client.post(
        f"/api/v1/users/{uid}:reset-password",
        json={"password": "new-pw-123"},
        headers=h,
    )
    assert resp.status_code == 204, resp.text
    assert await login("first-pw-1") == 401
    assert await login("new-pw-123") == 200


async def test_reset_password_unknown_user_404(client):
    h = await _admin(client)
    resp = await client.post(
        "/api/v1/users/u-nobody:reset-password",
        json={"password": "whatever-1"},
        headers=h,
    )
    assert resp.status_code == 404


async def test_reset_password_requires_admin(client):
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "svc", "password": "svc-pw"}
    )
    h = {"Authorization": f"Bearer {resp.json()['token']}"}
    resp = await client.post(
        "/api/v1/users/u-admin:reset-password",
        json={"password": "whatever-1"},
        headers=h,
    )
    assert resp.status_code == 403


async def test_reset_password_min_length_422(client):
    h = await _admin(client)
    resp = await client.post(
        "/api/v1/users/u-admin:reset-password",
        json={"password": "short"},
        headers=h,
    )
    assert resp.status_code == 422
