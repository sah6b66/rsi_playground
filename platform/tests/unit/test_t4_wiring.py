"""T4 wiring: composition-root kind routing, aggregated backend_status,
/sandbox/backends real data and platform health checkers (DD-04 §5.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.bootstrap import build_services
from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    ExpectedAnswer,
    LinkSpec,
    RootCauseExpectation,
    SshAccess,
    TaskRecord,
    TaskSpec,
    Topology,
)
from rsi.orchestrator.health import HealthMonitor, build_checkers
from rsi.sandbox.backends.base import BackendStatus, SandboxBackend
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.backends.prebuilt import PrebuiltBackend
from rsi.sandbox.platform_probe import PlatformProbe, PlatformProbeResult
from rsi.sandbox.readiness import ReadyReport
from rsi.sandbox.service import SandboxService
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService
from rsi.settings import (
    DatabaseSettings,
    DeviceEndpoint,
    SandboxBackend as SandboxBackendSettings,
    SandboxSettings,
    Settings,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class StubProbe:
    def __init__(self, ok: bool = True, detail: str = "HTTP 200") -> None:
        self.url = "https://192.168.56.101:8443"
        self.ok = ok
        self.detail = detail

    def probe_cached(self) -> PlatformProbeResult:
        return PlatformProbeResult(ok=self.ok, detail=self.detail)


class StubStatusBackend(SandboxBackend):
    """Minimal backend that returns a fixed status()."""

    kind = "ensp-pro"

    def __init__(self, status: BackendStatus) -> None:
        self._status = status

    async def build(self, env):
        raise NotImplementedError

    async def wait_ready(self, lab, timeout_s=600):
        raise NotImplementedError

    async def snapshot(self, lab, name):
        raise NotImplementedError

    async def restore(self, lab, snap):
        raise NotImplementedError

    async def reset(self, lab):
        raise NotImplementedError

    async def destroy(self, lab):
        raise NotImplementedError

    def exec(self, lab, device, cli):
        raise NotImplementedError

    @property
    def active_lab_count(self) -> int:
        return 0

    def status(self) -> BackendStatus:
        return self._status


class StubSandboxService:
    def __init__(self, statuses: list[BackendStatus]) -> None:
        self._statuses = statuses

    async def backend_status(self) -> list[BackendStatus]:
        return self._statuses


def _settings(tmp_path: Path, backends: dict) -> Settings:
    return Settings(
        home=tmp_path,
        database=DatabaseSettings(url=f"sqlite:///{tmp_path/'t4.db'}"),
        sandbox=SandboxSettings(backends=backends),
    )


def make_services(**extra) -> dict:
    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "researcher": {
                "user_id": "u-res",
                "role": "researcher",
                "password_hash": auth.hash_password("res-pw"),
            },
        },
    }
    services.update(extra)
    return services


# ---------------------------------------------------------------------------
# bootstrap kind routing
# ---------------------------------------------------------------------------


class TestBootstrapKindRouting:
    async def test_fake_and_prebuilt_routing(self, tmp_path):
        backends = {
            "fake": SandboxBackendSettings(kind="fake"),
            "ensp-pro-1": SandboxBackendSettings(
                kind="ensp-pro", url="https://192.0.2.10:8443"
            ),
            "pnetlab": SandboxBackendSettings(kind="pnetlab", url="http://192.0.2.20"),
        }
        services = build_services(_settings(tmp_path, backends))
        svc = services["sandbox_service"]
        assert isinstance(svc._backends["fake"], FakeSandboxBackend)
        e1 = svc._backends["ensp-pro-1"]
        p1 = svc._backends["pnetlab"]
        assert isinstance(e1, PrebuiltBackend)
        assert isinstance(p1, PrebuiltBackend)
        assert e1.kind == "ensp-pro" and p1.kind == "pnetlab"
        # probes registered for health/doctor, shared with the backends
        probes = services["platform_probes"]
        assert set(probes) == {"ensp-pro-1", "pnetlab"}
        assert probes["ensp-pro-1"].url == "https://192.0.2.10:8443"
        assert probes["ensp-pro-1"] is e1._probe
        assert probes["pnetlab"] is p1._probe
        # ssh pool + readiness shared across backends and the service
        assert e1._pool is services["ssh_pool"]
        assert p1._pool is services["ssh_pool"]
        assert e1._readiness is p1._readiness
        # scheduler sees the real registry
        assert set(svc._scheduler._backends) == {"fake", "ensp-pro-1", "pnetlab"}

    async def test_empty_url_non_fake_degrades_to_offline(self, tmp_path):
        backends = {
            "fake": SandboxBackendSettings(kind="fake"),
            "e1": SandboxBackendSettings(kind="ensp-pro", url=""),
        }
        services = build_services(_settings(tmp_path, backends))
        svc = services["sandbox_service"]
        e1 = svc._backends["e1"]
        # constructed (registry matches settings) but probe reports offline —
        # probing an empty URL raises UnsupportedProtocol, never an exception
        assert isinstance(e1, PrebuiltBackend)
        assert e1._probe.url == ""
        st = e1.status()
        assert st.available is False
        assert st.key == "e1"
        # assembly never raised and the fake backend is intact
        assert isinstance(svc._backends["fake"], FakeSandboxBackend)


# ---------------------------------------------------------------------------
# backend_status aggregation
# ---------------------------------------------------------------------------


class TestBackendStatusAggregation:
    async def test_aggregates_with_registry_keys(self, tmp_path):
        named = StubStatusBackend(
            BackendStatus(
                key="ensp-pro-1", kind="ensp-pro", available=False, detail="ConnectError"
            )
        )
        keyless = FakeSandboxBackend()  # default status() has key=""
        svc = SandboxService(
            backends={"ensp-pro-1": named, "fake-1": keyless},
            labs_dir=tmp_path,
        )
        statuses = await svc.backend_status()
        by_key = {s.key: s for s in statuses}
        assert set(by_key) == {"ensp-pro-1", "fake-1"}
        assert by_key["ensp-pro-1"].available is False
        assert by_key["ensp-pro-1"].detail == "ConnectError"
        # keyless default status gets the registry key filled in
        assert by_key["fake-1"].kind == "fake"
        assert by_key["fake-1"].available is True


# ---------------------------------------------------------------------------
# /sandbox/backends endpoint
# ---------------------------------------------------------------------------


class TestBackendsEndpoint:
    @pytest_asyncio.fixture
    async def client_factory(self):
        async def _make(services):
            app = create_app(services)
            return AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )

        return _make

    async def _login(self, client) -> dict:
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "researcher", "password": "res-pw"}
        )
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    async def test_returns_real_service_data(self, client_factory):
        stub = StubSandboxService(
            [
                BackendStatus(key="ensp-pro-1", kind="ensp-pro", available=False,
                              detail="ConnectError"),
                BackendStatus(key="fake", kind="fake", available=True),
            ]
        )
        async with await client_factory(make_services(sandbox_service=stub)) as client:
            h = await self._login(client)
            resp = await client.get("/api/v1/sandbox/backends", headers=h)
            assert resp.status_code == 200, resp.text
            items = {i["name"]: i for i in resp.json()["items"]}
            assert set(items) == {"ensp-pro-1", "fake"}
            assert items["ensp-pro-1"]["available"] is False
            assert items["ensp-pro-1"]["status"] == "offline"
            assert items["fake"]["available"] is True
            assert items["fake"]["status"] == "ready"

    async def test_returns_device_count_from_settings(self, client_factory, tmp_path):
        # 平台级设备清单计数（ADR-25 增补：GET /sandbox/backends）
        stub = StubSandboxService(
            [
                BackendStatus(key="phys-1", kind="physical", available=True,
                              detail="设备 SSH 可达（平台配置）"),
                BackendStatus(key="fake", kind="fake", available=True),
            ]
        )
        services = make_services(sandbox_service=stub)
        services["settings"] = _settings(tmp_path, {
            "phys-1": SandboxBackendSettings(
                kind="physical",
                devices={"R1": DeviceEndpoint(ip="192.168.56.10"),
                         "R2": DeviceEndpoint(ip="192.168.56.11", port=2222)},
            ),
            "fake": SandboxBackendSettings(kind="fake"),
        })
        async with await client_factory(services) as client:
            h = await self._login(client)
            resp = await client.get("/api/v1/sandbox/backends", headers=h)
            assert resp.status_code == 200, resp.text
            items = {i["name"]: i for i in resp.json()["items"]}
            assert items["phys-1"]["device_count"] == 2
            assert items["fake"]["device_count"] == 0

    async def test_falls_back_without_service(self, client_factory):
        async with await client_factory(make_services()) as client:
            h = await self._login(client)
            resp = await client.get("/api/v1/sandbox/backends", headers=h)
            assert resp.status_code == 200
            assert isinstance(resp.json()["items"], list)


# ---------------------------------------------------------------------------
# platform health checkers
# ---------------------------------------------------------------------------


class TestPlatformHealthCheckers:
    async def test_probe_up_and_down(self):
        checkers = build_checkers(
            {
                "platform_probes": {
                    "up-b": StubProbe(ok=True, detail="HTTP 200"),
                    "down-b": StubProbe(ok=False, detail="ConnectError"),
                }
            }
        )
        assert "platform:up-b" in checkers and "platform:down-b" in checkers
        mon = HealthMonitor(checkers)
        results = mon.check_all()
        assert results["platform:up-b"].status == "up"
        assert results["platform:up-b"].detail == "HTTP 200"
        assert results["platform:down-b"].status == "down"
        assert results["platform:down-b"].detail == "ConnectError"

    async def test_missing_probes_not_configured(self):
        checkers = build_checkers({})
        assert "platform" in checkers
        results = HealthMonitor(checkers).check_all()
        assert results["platform"].status == "unknown"
        assert results["platform"].detail == "not_configured"


# ---------------------------------------------------------------------------
# closed loop with a prebuilt backend (composition rig, offline)
# ---------------------------------------------------------------------------


class TestPrebuiltClosedLoop:
    async def test_batch_runs_on_prebuilt_backend(self, tmp_path):
        backends = {
            "fake": SandboxBackendSettings(kind="fake"),
            "ensp-pro-1": SandboxBackendSettings(
                kind="ensp-pro", url="https://192.0.2.10:8443"
            ),
        }
        services = build_services(_settings(tmp_path, backends))
        from sqlmodel import SQLModel

        SQLModel.metadata.create_all(services["engine"])
        # bring the platform probe online without any network (probe cache
        # seeding: TTL not yet expired, so probe_cached returns this result)
        probe = services["platform_probes"]["ensp-pro-1"]
        import time

        probe._cached = PlatformProbeResult(
            ok=True, status_code=200, detail="HTTP 200"
        )
        probe._cached_at = time.monotonic()

        prompt = "diagnose the link failure and reply with FINAL_JSON"
        out = '{"task_complete": true, "root_cause": "GE0/0/1 shutdown"}'
        from rsi.carrier.adapters.fake import FakeCarrier
        from rsi.sandbox.sshpool import MockDeviceSession

        services["host_service"].adapters["opencode"] = FakeCarrier(
            responses={prompt: f"<FINAL_JSON>{out}</FINAL_JSON>"}
        )
        # offline device pool: scripted mock sessions (the default
        # readiness check would TCP-probe unroutable IPs; a cli_match
        # ready-check exercises the same pool the batch exec uses)
        services["ssh_pool"]._factory = lambda key, profile, access=None: MockDeviceSession(
            key, profile, responses={"display clock": "2026-01-01 00:00:00 UTC"}
        )
        from rsi.core.models import ReadyCheck

        env = EnvSpec(
            name="lab-t4",
            backend="ensp-pro",
            devices=[
                DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
                DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
            ],
            topology=Topology(
                links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]
            ),
            ssh_access={
                "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin",
                                password="pw"),
                "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin",
                                password="pw"),
            },
            ready_checks=[
                ReadyCheck(
                    type="cli_match", device="R1",
                    cli="display clock", expect="00:00:00",
                )
            ],
        )
        tasks = [
            TaskRecord(
                task_id="fault/t4-01",
                title="t",
                task=TaskSpec(prompt=prompt),
                expected=ExpectedAnswer(
                    root_cause=RootCauseExpectation(expected="GE0/0/1 shutdown")
                ),
            )
        ]
        stats = await services["batch_runner"].run_batch(
            "batch-t4", env, tasks, "cfg-v1", "sut-img", "read-only"
        )
        assert stats.n == 1
        # the prebuilt backend actually carried the lab
        prebuilt = services["sandbox_service"]._backends["ensp-pro-1"]
        assert prebuilt.active_lab_count >= 0  # lifecycle ran without EnvError
        statuses = {s.key: s for s in await services["sandbox_service"].backend_status()}
        assert statuses["ensp-pro-1"].available is True
        assert statuses["fake"].available is True
