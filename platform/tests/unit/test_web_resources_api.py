"""T6 web 配套：镜像 / 执行主机 / 沙箱 PUT / 评测集克隆 端点。

按计划 Task 6 Interfaces 逐端点断言（stub host_pool docker client；
settings.yaml 写入用 tmp RSI_HOME；clone 用内存 store fixture）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.carrier.hosts import HostStatus
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.settings import Settings

pytestmark = pytest.mark.asyncio


# -- stub host pool -----------------------------------------------------------


class StubImages:
    def __init__(self, images: list | None = None, remove_error: Exception | None = None):
        self._images = images or []
        self._remove_error = remove_error
        self.load_calls: list[str] = []
        self.removed: list[str] = []

    def list(self) -> list:
        return self._images

    def load(self, fileobj) -> list:
        self.load_calls.append(getattr(fileobj, "name", "<file>"))
        fileobj.read()
        return []

    def remove(self, ref: str, **kw) -> dict:
        if self._remove_error is not None:
            raise self._remove_error
        self.removed.append(ref)
        return {"status": "deleted"}


class StubClient:
    def __init__(self, images: StubImages):
        self.images = images


class StubPool:
    """HostPool 替身：statuses() 返回固定主机，client() 按表返回/抛错。"""

    def __init__(self, clients: dict[str, StubClient | None], urls: dict[str, str]):
        self._clients = clients
        self._urls = urls

    def statuses(self):
        return [
            HostStatus(
                host=h,
                docker_url=self._urls.get(h, "local"),
                reachable=self._clients.get(h) is not None,
                instance_count=0,
                max_instances=2,
            )
            for h in self._urls
        ]

    def config(self, host: str):
        url = self._urls.get(host)
        if url is None:
            raise KeyError(host)
        return SimpleNamespace(docker=url, subnets=[], max_instances=None)

    def client(self, host: str):
        c = self._clients.get(host)
        if c is None:
            raise RuntimeError(f"host {host} offline")
        return c


def _image(ref: str, size: int = 500 * 1024 * 1024, created: str = "2026-11-01T00:00:00Z"):
    tags = [ref] if ref else []
    img_id = f"sha256:{ref.replace(':', '').replace('/', '')[:16]}"
    return SimpleNamespace(tags=tags, id=img_id, attrs={"Size": size, "Created": created})


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
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RSI_HOME", str(tmp_path))
    monkeypatch.delenv("RSI_SETTINGS", raising=False)
    images = StubImages(images=[_image("ipops-agent:v1.2"), _image("busybox:latest")])
    pool = StubPool(
        clients={"main": StubClient(images), "remote-1": None},
        urls={"main": "local", "remote-1": "ssh://user@192.168.1.50"},
    )
    app = create_app(make_services({"host_pool": pool, "settings": Settings()}))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c._app = app  # type: ignore[attr-defined]
        c._stub_images = images  # type: ignore[attr-defined]
        yield c


async def admin_headers(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ── GET/POST/DELETE /docker/images ─────────────────────────────────────────


class TestImages:
    async def test_list_grouped_by_host_with_used_by_and_offline(self, client):
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/suts",
            json={"name": "sut-img", "carrier": "linux",
                  "image_ref": "ipops-agent:v1.2"},
            headers=h,
        )
        sut_id = resp.json()["id"]
        resp = await client.get("/api/v1/docker/images", headers=h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        refs = {(i["host"], i["ref"]) for i in body["items"]}
        assert ("main", "ipops-agent:v1.2") in refs
        assert ("main", "busybox:latest") in refs
        assert body["offline_hosts"] == ["remote-1"]
        used = next(i for i in body["items"] if i["ref"] == "ipops-agent:v1.2")
        assert used["used_by"] == [sut_id]
        busybox = next(i for i in body["items"] if i["ref"] == "busybox:latest")
        assert busybox["used_by"] == []
        assert used["size_mb"] == 500.0
        assert used["created_at"] == "2026-11-01T00:00:00Z"

    async def test_list_without_host_pool_offline_all_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        monkeypatch.delenv("RSI_SETTINGS", raising=False)
        app = create_app(make_services({"settings": Settings()}))
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            h = await admin_headers(c)
            resp = await c.get("/api/v1/docker/images", headers=h)
        body = resp.json()
        assert body["items"] == []
        assert body["offline_hosts"] == ["main"]  # settings.carrier.hosts 全部键

    async def test_load_tar_success_202(self, client, tmp_path):
        h = await admin_headers(client)
        tar = tmp_path / "ipops-agent.tar"
        tar.write_bytes(b"tar-bytes")
        resp = await client.post(
            "/api/v1/docker/images:load",
            json={"host": "main", "tar_path": str(tar)},
            headers=h,
        )
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"status": "loaded", "host": "main"}
        assert client._stub_images.load_calls == [str(tar)]  # type: ignore[attr-defined]

    async def test_load_missing_tar_404(self, client, tmp_path):
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/docker/images:load",
            json={"host": "main", "tar_path": str(tmp_path / "nope.tar")},
            headers=h,
        )
        assert resp.status_code == 404
        assert "tar file not found on host" in resp.json()["message"]

    async def test_load_docker_error_502(self, client, tmp_path):
        h = await admin_headers(client)
        tar = tmp_path / "bad.tar"
        tar.write_bytes(b"x")

        def boom(fileobj):
            raise RuntimeError("daemon down")

        client._stub_images.load = boom  # type: ignore[attr-defined]
        resp = await client.post(
            "/api/v1/docker/images:load",
            json={"host": "main", "tar_path": str(tar)},
            headers=h,
        )
        assert resp.status_code == 502
        assert "daemon down" in resp.json()["message"]

    async def test_delete_referenced_409(self, client):
        h = await admin_headers(client)
        sut_id = (await client.post(
            "/api/v1/suts",
            json={"name": "sut-del", "carrier": "linux",
                  "image_ref": "ipops-agent:v1.2"},
            headers=h,
        )).json()["id"]
        resp = await client.delete(
            "/api/v1/docker/images/ipops-agent:v1.2?host=main", headers=h
        )
        assert resp.status_code == 409
        assert sut_id in str(resp.json())

    async def test_delete_unused_204_then_unknown_404(self, client):
        h = await admin_headers(client)
        resp = await client.delete(
            "/api/v1/docker/images/busybox:latest?host=main", headers=h
        )
        assert resp.status_code == 204
        assert client._stub_images.removed == ["busybox:latest"]  # type: ignore[attr-defined]
        # daemon 报 No such image → 404
        client._stub_images._remove_error = RuntimeError("No such image: x")  # type: ignore[attr-defined]
        resp = await client.delete(
            "/api/v1/docker/images/ghost:1?host=main", headers=h
        )
        assert resp.status_code == 404


# ── GET/PUT /carrier/hosts ─────────────────────────────────────────────────


class TestCarrierHosts:
    async def test_get_aggregates_settings_and_pool(self, client):
        h = await admin_headers(client)
        resp = await client.get("/api/v1/carrier/hosts", headers=h)
        assert resp.status_code == 200, resp.text
        items = {i["key"]: i for i in resp.json()["items"]}
        assert items["main"]["mode"] == "local"
        assert items["main"]["endpoint"] == "local"
        assert items["remote-1"]["mode"] == "remote"
        assert items["remote-1"]["endpoint"] == "ssh://user@192.168.1.50"
        assert items["main"]["online"] is True
        assert items["remote-1"]["online"] is False
        assert items["main"]["max_instances"] > 0
        assert "subnets" in items["main"]

    async def test_put_writes_settings_yaml_carrier_section(self, client, tmp_path):
        # 预置既有 settings.yaml（含需保留的其他段）
        yml = tmp_path / "settings.yaml"
        yml.write_text(
            "seed:\n  master: 20261102\ncarrier:\n  hosts:\n"
            "    main:\n      docker: local\n",
            encoding="utf-8",
        )
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/carrier/hosts",
            json={
                "items": [
                    {
                        "key": "r2",
                        "endpoint": "ssh://user@10.0.0.9",
                        "max_instances": 4,
                        "subnets": ["10.103.0.0/16"],
                    },
                    {"key": "main", "endpoint": "local", "subnets": []},
                ]
            },
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"restart_required": True}
        doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
        assert doc["seed"]["master"] == 20261102  # 其他段保留
        hosts = doc["carrier"]["hosts"]
        assert hosts["r2"]["docker"] == "ssh://user@10.0.0.9"
        assert hosts["r2"]["max_instances"] == 4
        assert hosts["r2"]["subnets"] == ["10.103.0.0/16"]
        assert hosts["main"]["docker"] == "local"


# ── PUT /sandbox/backends/{key} ─────────────────────────────────────────────


class TestSandboxBackendPut:
    async def test_put_writes_sandbox_backend_section(self, client, tmp_path):
        yml = tmp_path / "settings.yaml"
        yml.write_text(
            "sandbox:\n  backends:\n    fake:\n      kind: fake\n",
            encoding="utf-8",
        )
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/sandbox/backends/ensp-pro-2",
            json={
                "kind": "ensp-pro",
                "url": "https://192.168.56.201:8443",
                "verify_tls": False,
                "subnets": ["10.104.0.0/16"],
            },
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"restart_required": True}
        doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
        assert doc["sandbox"]["backends"]["fake"]["kind"] == "fake"  # 既有保留
        b = doc["sandbox"]["backends"]["ensp-pro-2"]
        assert b == {
            "kind": "ensp-pro",
            "url": "https://192.168.56.201:8443",
            "verify_tls": False,
            "subnets": ["10.104.0.0/16"],
        }

    async def test_put_accepts_physical_kind_without_url(self, client, tmp_path):
        # 物理网络类型（ADR-25 增补）：无 URL，在线判定走设备 SSH 回退
        yml = tmp_path / "settings.yaml"
        yml.write_text("", encoding="utf-8")
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/sandbox/backends/phys-1",
            json={"kind": "physical", "url": "", "verify_tls": False, "subnets": []},
            headers=h,
        )
        assert resp.status_code == 200, resp.text
        doc = yaml.safe_load(yml.read_text(encoding="utf-8"))
        assert doc["sandbox"]["backends"]["phys-1"]["kind"] == "physical"


# ── POST /evalsets/{id}:clone ───────────────────────────────────────────────


ENV_YAML = """\
name: bgp-site
backend: ensp-pro
devices:
  - {name: R1, type: vrp, config: configs/R1.cfg}
ssh_access:
  R1: {ip: 192.168.56.10, port: 22, username: admin}
config_apis:
  nms: {base_url: http://192.168.56.20/api, timeout_s: 60}
"""


async def _seed_evalset(client, h) -> str:
    stores = client._app.state.stores  # type: ignore[attr-defined]
    stores["evalsets"]["es-src"] = {
        "id": "es-src",
        "name": "netops-bgp",
        "purpose": "iterate",
        "tasks": [{"id": "t-001", "prompt": "p1"}, {"id": "t-002", "prompt": "p2"}],
        "description": "",
        "env_yaml": ENV_YAML,
        "configs": {"R1.cfg": "sysname R1"},
        "frozen": True,
        "version": 1,
        "created_at": "2026-11-01T00:00:00Z",
        "versions": [],
    }
    return "es-src"


class TestEvalsetClone:
    async def test_clone_applies_device_endpoint_overrides(self, client):
        h = await admin_headers(client)
        src_id = await _seed_evalset(client, h)
        resp = await client.post(
            f"/api/v1/evalsets/{src_id}:clone",
            json={
                "new_name": "netops-bgp-site2",
                "overrides": {
                    "device_endpoints": {"R1": {"ip": "10.201.0.10", "port": 2222}},
                    "config_apis": [
                        {"name": "nms", "base_url": "http://10.201.0.20/api",
                         "timeout_s": 30}
                    ],
                    "site_name": "site2",
                },
            },
            headers=h,
        )
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["name"] == "netops-bgp-site2"
        assert out["frozen"] is False
        assert out["tasks"] == [
            {"id": "t-001", "prompt": "p1"}, {"id": "t-002", "prompt": "p2"}
        ]
        stores = client._app.state.stores  # type: ignore[attr-defined]
        new = stores["evalsets"][out["id"]]
        assert new is not stores["evalsets"]["es-src"]
        env = yaml.safe_load(new["env_yaml"])
        assert env["ssh_access"]["R1"]["ip"] == "10.201.0.10"
        assert env["ssh_access"]["R1"]["port"] == 2222
        assert env["config_apis"]["nms"]["base_url"] == "http://10.201.0.20/api"
        assert new["configs"] == {"R1.cfg": "sysname R1"}  # configs 复制
        assert new.get("overrides", {}).get("site_name") == "site2"
        # 源记录不被修改
        src = yaml.safe_load(stores["evalsets"]["es-src"]["env_yaml"])
        assert src["ssh_access"]["R1"]["ip"] == "192.168.56.10"

    async def test_clone_duplicate_name_409(self, client):
        h = await admin_headers(client)
        await _seed_evalset(client, h)
        resp = await client.post(
            "/api/v1/evalsets/es-src:clone",
            json={"new_name": "netops-bgp", "overrides": {}},
            headers=h,
        )
        assert resp.status_code == 409

    async def test_clone_unknown_404(self, client):
        h = await admin_headers(client)
        resp = await client.post(
            "/api/v1/evalsets/es-nope:clone",
            json={"new_name": "x", "overrides": {}},
            headers=h,
        )
        assert resp.status_code == 404


class TestSandboxBackendPutValidation:
    async def test_put_invalid_kind_422(self, client):
        h = await admin_headers(client)
        resp = await client.put(
            "/api/v1/sandbox/backends/bogus-1",
            json={"kind": "gns3", "url": "", "verify_tls": False, "subnets": []},
            headers=h,
        )
        assert resp.status_code == 422, resp.text
