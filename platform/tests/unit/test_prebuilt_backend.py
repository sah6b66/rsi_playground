"""Unit tests: PrebuiltBackend (T3, DD-04 §5.7 prebuilt mode)."""

from __future__ import annotations

import socket

import pytest

from rsi.core.errors import EnvError
from rsi.core.models import DeviceSpec, EnvSpec, LinkSpec, SshAccess, Topology
from rsi.sandbox.backends import prebuilt
from rsi.sandbox.backends.prebuilt import PrebuiltBackend
from rsi.sandbox.platform_probe import PlatformProbeResult
from rsi.sandbox.readiness import ReadyReport
from rsi.sandbox.scheduler import LabScheduler
from rsi.sandbox.sshpool import ExecResult, SessionKey
from rsi.sandbox.vendor import VENDOR_MAP

# ---------------------------------------------------------------------------
# stubs


class StubProbe:
    def __init__(self, ok: bool = True, detail: str = "HTTP 200") -> None:
        self.url = "https://192.168.56.101:8443"
        self.ok = ok
        self.detail = detail
        self.calls = 0

    def probe_cached(self) -> PlatformProbeResult:
        self.calls += 1
        return PlatformProbeResult(ok=self.ok, detail=self.detail)


class StubSession:
    def __init__(self, output: str = "ok", ok: bool = True) -> None:
        self.output = output
        self.ok = ok
        self.cli_calls: list[str] = []
        self.config_calls: list[list[str]] = []

    async def exec_cli(self, cli: str, read_timeout_s: int = 120) -> ExecResult:
        self.cli_calls.append(cli)
        return ExecResult(ok=self.ok, output=self.output)

    async def exec_config(self, lines: list[str]) -> ExecResult:
        self.config_calls.append(list(lines))
        return ExecResult(ok=self.ok, output=self.output)


class StubPool:
    """Records device()/close_lab() calls; returns per-endpoint sessions."""

    def __init__(self, fail_devices: bool = False) -> None:
        self.device_calls: list[tuple[SessionKey, SshAccess, str]] = []
        self.closed_labs: list[str] = []
        self.sessions: list[StubSession] = []
        self.fail_devices = fail_devices

    async def device(self, key: SessionKey, access: SshAccess, vendor: str):
        self.device_calls.append((key, access, vendor))
        if self.fail_devices:
            raise ConnectionError(f"ssh unreachable {key.ip}:{key.port}")
        session = StubSession()
        self.sessions.append(session)
        return session

    async def close_lab(self, lab_id: str) -> None:
        self.closed_labs.append(lab_id)


class StubReadiness:
    def __init__(self) -> None:
        self.calls: list[tuple[EnvSpec, dict, int]] = []

    async def check(self, env, endpoints, timeout_s: int = 600) -> ReadyReport:
        self.calls.append((env, endpoints, timeout_s))
        return ReadyReport(passed=True)


def make_env() -> EnvSpec:
    return EnvSpec(
        name="prebuilt-env",
        backend="ensp-pro",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="ios", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/1"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
    )


_UNSET = object()


def make_backend(probe=None, pool=_UNSET, readiness=None, devices=None):
    return PrebuiltBackend(
        key="ensp-pro-1",
        kind="ensp-pro",
        probe=probe or StubProbe(),
        ssh_pool=StubPool() if pool is _UNSET else pool,
        readiness=readiness or StubReadiness(),
        devices=devices,
    )


# ---------------------------------------------------------------------------
# build


class TestBuild:
    @pytest.mark.asyncio
    async def test_build_passes_through_endpoints(self):
        probe, pool = StubProbe(), StubPool()
        backend = make_backend(probe=probe, pool=pool)
        env = make_env()
        lab = await backend.build(env)
        assert lab.endpoints == env.ssh_access
        assert lab.backend == "ensp-pro"
        assert lab.host == "192.168.56.101"  # hostname of the platform URL
        assert backend.active_lab_count == 1
        assert pool.device_calls == []  # nothing provisioned, nothing connected

    @pytest.mark.asyncio
    async def test_build_offline_platform_raises_with_url(self):
        probe = StubProbe(ok=False, detail="ConnectError")
        backend = make_backend(probe=probe)
        with pytest.raises(EnvError) as exc_info:
            await backend.build(make_env())
        msg = str(exc_info.value)
        assert probe.url in msg
        assert exc_info.value.context["url"] == probe.url
        assert exc_info.value.context["detail"] == "ConnectError"
        assert backend.active_lab_count == 0


# ---------------------------------------------------------------------------
# wait_ready


class TestWaitReady:
    @pytest.mark.asyncio
    async def test_wait_ready_delegates_to_readiness(self):
        readiness = StubReadiness()
        backend = make_backend(readiness=readiness)
        env = make_env()
        lab = await backend.build(env)
        report = await backend.wait_ready(lab, timeout_s=42)
        assert report.passed is True
        assert len(readiness.calls) == 1
        checked_env, endpoints, timeout_s = readiness.calls[0]
        assert checked_env is env
        assert endpoints == lab.endpoints
        assert timeout_s == 42

    @pytest.mark.asyncio
    async def test_wait_ready_unknown_lab_raises(self):
        backend = make_backend()
        unknown = await make_backend().build(make_env())
        with pytest.raises(EnvError, match="unknown lab"):
            await backend.wait_ready(unknown)


# ---------------------------------------------------------------------------
# snapshot / restore


class TestSnapshotRestore:
    @pytest.mark.asyncio
    async def test_snapshot_is_noop(self):
        backend = make_backend()
        lab = await backend.build(make_env())
        snap = await backend.snapshot(lab, "snap0")
        assert snap.impl_kind == "noop"
        assert snap.backend_ref == "snap0"

    @pytest.mark.asyncio
    async def test_restore_warns_without_raising(self, caplog):
        backend = make_backend()
        lab = await backend.build(make_env())
        snap = await backend.snapshot(lab, "snap0")
        await backend.restore(lab, snap)  # must not raise


# ---------------------------------------------------------------------------
# reset


class TestReset:
    @pytest.mark.asyncio
    async def test_reset_runs_vendor_reset_cmds_per_device(self):
        pool = StubPool()
        backend = make_backend(pool=pool)
        lab = await backend.build(make_env())
        await backend.reset(lab)
        assert len(pool.device_calls) == 2
        vendors = {c[2] for c in pool.device_calls}
        assert vendors == {"vrp", "ios"}
        # key alignment: (ip, port) from env.ssh_access, lab_id for tracing
        ips = {(c[0].ip, c[0].port) for c in pool.device_calls}
        assert ips == {("10.0.0.1", 2001), ("10.0.0.2", 2002)}
        assert all(c[0].lab_id == lab.lab_id for c in pool.device_calls)
        cmds = [tuple(s.config_calls[0]) for s in pool.sessions]
        assert tuple(VENDOR_MAP["vrp"].reset_cmd) in cmds
        assert tuple(VENDOR_MAP["ios"].reset_cmd) in cmds

    @pytest.mark.asyncio
    async def test_reset_without_pool_does_not_raise(self):
        backend = make_backend(pool=None)
        lab = await backend.build(make_env())
        await backend.reset(lab)  # no pool wired — skip with warning

    @pytest.mark.asyncio
    async def test_reset_skips_device_without_ssh_access(self):
        pool = StubPool()
        backend = make_backend(pool=pool)
        env = make_env()
        env.ssh_access.pop("R2")
        lab = await backend.build(env)
        await backend.reset(lab)
        assert len(pool.device_calls) == 1
        assert pool.device_calls[0][0].ip == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_reset_cmd_override_table(self):
        pool = StubPool()
        backend = PrebuiltBackend(
            key="ensp-pro-1",
            kind="ensp-pro",
            probe=StubProbe(),
            ssh_pool=pool,
            readiness=StubReadiness(),
            reset_cmds={"vrp": ["undo everything"]},
        )
        lab = await backend.build(make_env())
        await backend.reset(lab)
        all_cmds = [line for s in pool.sessions for call in s.config_calls for line in call]
        assert "undo everything" in all_cmds
        assert tuple(VENDOR_MAP["ios"].reset_cmd) == ("write erase",)
        assert "write erase" in all_cmds  # ios falls back to vendor table


# ---------------------------------------------------------------------------
# destroy


class TestDestroy:
    @pytest.mark.asyncio
    async def test_destroy_clears_env_and_pool(self):
        pool = StubPool()
        backend = make_backend(pool=pool)
        lab = await backend.build(make_env())
        await backend.destroy(lab)
        assert backend.active_lab_count == 0
        assert pool.closed_labs == [lab.lab_id]

    @pytest.mark.asyncio
    async def test_destroy_without_pool_does_not_raise(self):
        backend = make_backend(pool=None)
        lab = await backend.build(make_env())
        await backend.destroy(lab)
        assert backend.active_lab_count == 0


# ---------------------------------------------------------------------------
# exec


class TestExec:
    @pytest.mark.asyncio
    async def test_exec_via_pool_session(self):
        pool = StubPool()
        backend = make_backend(pool=pool)
        lab = await backend.build(make_env())
        output = backend.exec(lab, "R1", "display clock")
        assert output == "ok"
        assert len(pool.device_calls) == 1
        key, access, vendor = pool.device_calls[0]
        assert (key.ip, key.port) == ("10.0.0.1", 2001)  # same key as _pool_session
        assert key.lab_id == lab.lab_id
        assert access is lab.endpoints["R1"]
        assert vendor == "vrp"
        assert pool.sessions[0].cli_calls == ["display clock"]

    @pytest.mark.asyncio
    async def test_exec_without_pool_raises(self):
        backend = make_backend(pool=None)
        lab = await backend.build(make_env())
        with pytest.raises(EnvError, match="no ssh pool"):
            backend.exec(lab, "R1", "display clock")

    @pytest.mark.asyncio
    async def test_exec_pool_failure_raises_enverror(self):
        backend = make_backend(pool=StubPool(fail_devices=True))
        lab = await backend.build(make_env())
        with pytest.raises(EnvError):
            backend.exec(lab, "R1", "display clock")

    @pytest.mark.asyncio
    async def test_exec_unknown_device_raises(self):
        backend = make_backend()
        lab = await backend.build(make_env())
        with pytest.raises(EnvError):
            backend.exec(lab, "R9", "display clock")


# ---------------------------------------------------------------------------
# status


class TestStatus:
    def test_status_online(self):
        probe = StubProbe(ok=True, detail="HTTP 200")
        backend = make_backend(probe=probe)
        status = backend.status()
        assert status.key == "ensp-pro-1"
        assert status.kind == "ensp-pro"
        assert status.available is True
        assert status.detail == "HTTP 200"
        assert status.active_labs == 0

    def test_status_offline(self):
        probe = StubProbe(ok=False, detail="ConnectError")
        backend = make_backend(probe=probe)
        status = backend.status()
        assert status.available is False
        assert status.detail == "ConnectError"


# ---------------------------------------------------------------------------
# status 在线判定增补（ADR-25）：未配置 URL → 设备 SSH 可达回退


class TestStatusNoUrlFallback:
    def _no_url_backend(self):
        probe = StubProbe()
        probe.url = ""  # 未配置平台 URL（物理网络）
        return make_backend(probe=probe)

    @pytest.mark.asyncio
    async def test_no_url_with_reachable_device_is_online(self):
        # 本地起一个 TCP listener 模拟可达设备 SSH 端口
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            backend = self._no_url_backend()
            base = make_env()
            env = base.model_copy(
                update={
                    "ssh_access": {
                        "R1": SshAccess(
                            ip="127.0.0.1", port=port, username="admin", password="pw"
                        ),
                        "R2": SshAccess(
                            ip="127.0.0.1", port=1, username="admin", password="pw"
                        ),  # 不可达端口——任一可达即可
                    }
                }
            )
            await backend.build(env)
            status = backend.status()
            assert status.available is True
            assert status.detail == "设备 SSH 可达"
            assert status.active_labs == 1

    @pytest.mark.asyncio
    async def test_no_url_without_labs_is_offline(self):
        backend = self._no_url_backend()
        status = backend.status()
        assert status.available is False
        assert status.detail == "未配置 URL 且无设备信息"

    @pytest.mark.asyncio
    async def test_no_url_unreachable_devices_are_offline(self, monkeypatch):
        monkeypatch.setattr(
            prebuilt, "tcp_reachable", lambda ip, port: False
        )
        backend = self._no_url_backend()
        await backend.build(make_env())
        status = backend.status()
        assert status.available is False
        assert status.detail == "未配置 URL 且无设备信息"

    # ── 平台级设备清单（ADR-25 增补：监控与 lab 无关）─────────────────

    @pytest.mark.asyncio
    async def test_no_url_platform_devices_reachable_is_online(self, monkeypatch):
        # ① 平台级 devices 任一可达 → 在线（无需任何在用 lab）
        called: list[tuple[str, int]] = []

        def fake_tcp(ip: str, port: int) -> bool:
            called.append((ip, port))
            return ip == "192.168.56.10"

        monkeypatch.setattr(prebuilt, "tcp_reachable", fake_tcp)
        backend = make_backend(probe=self._no_url_probe(), devices={
            "R1": prebuilt.DeviceEndpoint(ip="192.168.56.11", port=22),
            "R2": prebuilt.DeviceEndpoint(ip="192.168.56.10", port=22),
        })
        status = backend.status()
        assert status.available is True
        assert status.detail == "设备 SSH 可达（平台配置）"
        assert status.active_labs == 0  # 未跑评测也在线
        assert ("192.168.56.11", 22) in called  # 确实在探测平台级清单

    @pytest.mark.asyncio
    async def test_no_url_platform_devices_unreachable_falls_back_to_lab(self):
        # ② 平台级全不可达 → 回退在用 lab 端点
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            backend = make_backend(
                probe=self._no_url_probe(),
                devices={"R1": prebuilt.DeviceEndpoint(ip="192.168.56.99", port=22)},
            )
            env = make_env().model_copy(
                update={
                    "ssh_access": {
                        "R1": SshAccess(
                            ip="127.0.0.1", port=port, username="admin", password="pw"
                        )
                    }
                }
            )
            await backend.build(env)
            status = backend.status()
            assert status.available is True
            assert status.detail == "设备 SSH 可达"

    def _no_url_probe(self):
        probe = StubProbe()
        probe.url = ""
        return probe

    @pytest.mark.asyncio
    async def test_build_without_url_skips_platform_probe(self):
        # 物理网络：无 URL 也可 build（端点透传），可达性交给 wait_ready
        probe = StubProbe()
        probe.url = ""
        backend = make_backend(probe=probe)
        lab = await backend.build(make_env())
        assert lab.endpoints == make_env().ssh_access
        assert lab.host == ""


# ---------------------------------------------------------------------------
# scheduler availability filter


class TestSchedulerAvailability:
    def test_select_skips_unavailable_backend(self):
        env = make_env()
        up = make_backend(probe=StubProbe(ok=True), pool=StubPool())
        down = make_backend(probe=StubProbe(ok=False))
        sched = LabScheduler({"b-down": down, "b-up": up})
        sel = sched.select(env)
        assert sel.backend_key == "b-up"

    def test_select_all_unavailable_raises(self):
        env = make_env()
        down1 = make_backend(probe=StubProbe(ok=False))
        down2 = make_backend(probe=StubProbe(ok=False))
        sched = LabScheduler({"b1": down1, "b2": down2})
        with pytest.raises(EnvError, match="unavailable"):
            sched.select(env)

    def test_select_explicit_unavailable_raises(self):
        env = make_env()
        down = make_backend(probe=StubProbe(ok=False))
        up = make_backend(probe=StubProbe(ok=True))
        sched = LabScheduler({"b1": down, "b2": up})
        with pytest.raises(EnvError, match="unavailable"):
            sched.select(env, explicit_key="b1")

    def test_select_explicit_available_still_wins(self):
        env = make_env()
        up1 = make_backend(probe=StubProbe(ok=True))
        up2 = make_backend(probe=StubProbe(ok=True))
        sched = LabScheduler({"b1": up1, "b2": up2})
        sel = sched.select(env, explicit_key="b2")
        assert sel.backend_key == "b2"
