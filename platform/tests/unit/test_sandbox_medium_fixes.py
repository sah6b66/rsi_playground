"""Unit tests: audit Medium fixes M1 (SshPool wiring) + M6 (client reuse).

All offline: the SshPool is given a session_factory returning
MockDeviceSession, and the ConfigApiCaller is driven against
httpx.MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from rsi.core.models import (
    CallApiParams,
    CliEntry,
    ConfigApi,
    DeviceSpec,
    EnvSpec,
    LinkSpec,
    SshAccess,
    Topology,
)
from rsi.sandbox.backends.fake import FakeSandboxBackend
from rsi.sandbox.config_api import ConfigApiCaller
from rsi.sandbox.service import SandboxService
from rsi.sandbox.sshpool import (
    MockDeviceSession,
    SessionKey,
    SshPool,
    VendorProfile,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _env(name: str = "lab-m") -> EnvSpec:
    return EnvSpec(
        name=name,
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="vrp", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.1.0.1", port=2101, username="admin", password="pw"),
            "R2": SshAccess(ip="10.1.0.2", port=2102, username="admin", password="pw"),
        },
        config_apis={"nms": ConfigApi(base_url="http://nms.invalid/api")},
    )


def _mock_responses() -> dict[str, str]:
    return {
        "display clock": "2026-01-01 00:00:00\nUTC\n",
        "display version": "VRP (R) software, Version 8.180 (pool-mock)\n",
    }


def _pool() -> tuple[SshPool, list[MockDeviceSession]]:
    made: list[MockDeviceSession] = []

    def factory(key: SessionKey, vendor: VendorProfile) -> MockDeviceSession:
        session = MockDeviceSession(key, vendor, responses=_mock_responses())
        made.append(session)
        return session

    return SshPool(session_factory=factory), made


async def _build(service: SandboxService, env: EnvSpec | None = None):
    return await service.build_lab(env or _env())


# ---------------------------------------------------------------------------
# M1: exec_readonly routing
# ---------------------------------------------------------------------------


class TestExecReadonlyRouting:
    @pytest.mark.asyncio
    async def test_exec_readonly_via_pool_hits_device_session(self, tmp_path):
        pool, sessions = _pool()
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()}, labs_dir=tmp_path, ssh_pool=pool
        )
        lab = await _build(service)
        out = service.exec_readonly(lab, "R1", "display clock")
        assert "2026-01-01" in out
        assert sessions, "pool session was created"
        assert "display clock" in sessions[0].sent

    @pytest.mark.asyncio
    async def test_exec_readonly_without_pool_falls_back(self, tmp_path):
        backend = FakeSandboxBackend()
        service = SandboxService(backends={"fake-1": backend}, labs_dir=tmp_path)
        lab = await _build(service)
        out = service.exec_readonly(lab, "R1", "display clock")
        # FakeDevice returns the real UTC clock; pool path would have
        # returned the canned "2026-01-01" mock response.
        assert "2026-01-01" not in out
        assert "UTC" in out

    @pytest.mark.asyncio
    async def test_exec_readonly_no_ssh_context_falls_back(self, tmp_path):
        pool, sessions = _pool()
        backend = FakeSandboxBackend()
        service = SandboxService(
            backends={"fake-1": backend}, labs_dir=tmp_path, ssh_pool=pool
        )
        lab = await _build(service)
        # simulate a lab whose env mapping is unavailable to the pool router
        # (device ssh context cannot be constructed) -> backend fallback
        service._envs.pop(lab.lab_id)
        out = service.exec_readonly(lab, "R1", "display clock")
        assert "UTC" in out
        assert not sessions, "pool session must not be created on fallback"


# ---------------------------------------------------------------------------
# M1: close()
# ---------------------------------------------------------------------------


class TestServiceClose:
    @pytest.mark.asyncio
    async def test_close_closes_pool(self, tmp_path):
        closed = {"n": 0}

        class StubPool(SshPool):
            async def close_all(self) -> None:  # type: ignore[override]
                closed["n"] += 1

        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()},
            labs_dir=tmp_path,
            ssh_pool=StubPool(),
        )
        service.close()
        assert closed["n"] == 1
        service.close()  # idempotent
        assert closed["n"] == 1


# ---------------------------------------------------------------------------
# M1: health_check via pool
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_via_pool(self, tmp_path):
        pool, sessions = _pool()
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()}, labs_dir=tmp_path, ssh_pool=pool
        )
        lab = await _build(service)
        assert await service.health_check(lab) is True
        assert sessions and "display clock" in sessions[0].sent

    @pytest.mark.asyncio
    async def test_health_check_via_pool_failure(self, tmp_path):
        def factory(key, vendor):
            return MockDeviceSession(key, vendor, responses={}, default_response="Error: down")

        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()},
            labs_dir=tmp_path,
            ssh_pool=SshPool(session_factory=factory),
        )
        lab = await _build(service)
        assert await service.health_check(lab) is False


# ---------------------------------------------------------------------------
# M6: ConfigApiCaller client reuse
# ---------------------------------------------------------------------------


def _params() -> CallApiParams:
    return CallApiParams(
        env_name="lab-m",
        scenario="sc",
        devices=[CliEntry(device="R1", cli_lines=["sysname X"])],
    )


def _mock_transport_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestConfigApiClientReuse:
    @pytest.mark.asyncio
    async def test_config_api_client_reuse(self):
        created: list[httpx.AsyncClient] = []
        real_ctor = httpx.AsyncClient

        def counting_ctor(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                lambda r: httpx.Response(200, json={"ok": True})
            )
            client = real_ctor(*args, **kwargs)
            created.append(client)
            return client

        caller = ConfigApiCaller()
        env = _env()
        import rsi.sandbox.config_api as ca

        orig = ca.httpx.AsyncClient
        ca.httpx.AsyncClient = counting_ctor
        try:
            await caller.apply(env, "nms", _params())
            first = caller._client
            await caller.apply(env, "nms", _params(), undo=True)
        finally:
            ca.httpx.AsyncClient = orig
        assert len(created) == 1
        assert caller._client is first
        await caller.aclose()
        assert caller._client is None

    @pytest.mark.asyncio
    async def test_config_api_injected_client_not_closed(self):
        client = _mock_transport_client()
        caller = ConfigApiCaller(client=client)
        await caller.apply(_env(), "nms", _params())
        await caller.aclose()
        # external client still usable (not closed by aclose)
        resp = await client.get("http://nms.invalid/healthz")
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_service_close_closes_owned_config_api_client(self, tmp_path):
        caller = ConfigApiCaller()
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()}, labs_dir=tmp_path, config_api=caller
        )
        # force lazy client creation
        ca_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={})
        ))
        caller._client = ca_client
        caller._owns_client = True
        service.close()
        assert caller._client is None


# ---------------------------------------------------------------------------
# H1: health_check must share the runner loop with exec_readonly
# ---------------------------------------------------------------------------


class _LoopRecordingSession:
    """Session whose exec_cli records the loop it runs on."""

    def __init__(self) -> None:
        self.loops: list[int] = []

    async def exec_cli(self, cli: str):
        import asyncio

        self.loops.append(id(asyncio.get_running_loop()))

        class R:
            ok = True
            output = "2026-01-01 00:00:00\nUTC\n"

        return R()


class _StubPool(SshPool):
    def __init__(self, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self._session = session

    async def device(self, key, access, vendor):  # type: ignore[override]
        return self._session

    async def close_all(self) -> None:
        return None


class TestHealthCheckLoopAffinity:
    @pytest.mark.asyncio
    async def test_health_check_uses_runner_loop_same_as_exec_readonly(self, tmp_path):
        session = _LoopRecordingSession()
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()},
            labs_dir=tmp_path,
            ssh_pool=_StubPool(session),
        )
        lab = await _build(service)
        service.exec_readonly(lab, "R1", "display clock")
        assert await service.health_check(lab) is True
        assert len(session.loops) == 2
        # both paths ran on the same (runner) loop — the caller's pytest
        # loop differs, proving health_check no longer awaits directly.
        assert session.loops[0] == session.loops[1]
        import asyncio

        assert session.loops[0] != id(asyncio.get_running_loop())


# ---------------------------------------------------------------------------
# M2b: close() must not leak the config client when pool close fails
# ---------------------------------------------------------------------------


class TestCloseIndependence:
    @pytest.mark.asyncio
    async def test_pool_close_failure_still_closes_config_api(self, tmp_path):
        class BoomPool(SshPool):
            async def close_all(self) -> None:  # type: ignore[override]
                raise RuntimeError("pool boom")

        caller = ConfigApiCaller()
        ca_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        )
        caller._client = ca_client
        caller._owns_client = True
        service = SandboxService(
            backends={"fake-1": FakeSandboxBackend()},
            labs_dir=tmp_path,
            ssh_pool=BoomPool(),
            config_api=caller,
        )
        service.close()  # must not raise
        assert caller._client is None
