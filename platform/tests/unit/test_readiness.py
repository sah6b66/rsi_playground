"""Unit tests: ReadinessChecker (DD-04 §4 wait_ready / §5 readiness)."""

from __future__ import annotations

import pytest

from rsi.core.models import (
    DeviceSpec,
    EnvSpec,
    LinkSpec,
    ReadyCheck,
    SshAccess,
    Topology,
)
from rsi.sandbox.readiness import CheckResult, ReadinessChecker, ReadyReport
from rsi.sandbox.sshpool import MockDeviceSession, SshPool


def _env(ready_checks: list[ReadyCheck] | None = None) -> EnvSpec:
    return EnvSpec(
        name="lab-a",
        backend="fake",
        devices=[
            DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg"),
            DeviceSpec(name="R2", type="ios", config="configs/R2.cfg"),
        ],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/1", "R2:GE0/0/0"))]),
        ssh_access={
            "R1": SshAccess(ip="10.0.0.1", port=2001, username="admin", password="pw"),
            "R2": SshAccess(ip="10.0.0.2", port=2002, username="admin", password="pw"),
        },
        ready_checks=ready_checks or [],
    )


def _endpoints(env: EnvSpec) -> dict[str, SshAccess]:
    return dict(env.ssh_access)


async def _ok_probe(ip: str, port: int) -> bool:
    return True


async def _fail_probe(ip: str, port: int) -> bool:
    return False


class TestCheckResultModels:
    def test_ready_report_shape(self):
        r = ReadyReport(
            passed=True,
            checks=[CheckResult(type="ssh_reachable", device="R1", passed=True, detail="ok")],
            elapsed_s=0.1,
        )
        assert r.passed and r.checks[0].passed and r.elapsed_s >= 0


class TestSshReachable:
    @pytest.mark.asyncio
    async def test_default_all_devices_when_no_ready_checks(self):
        env = _env()
        checker = ReadinessChecker(tcp_probe=_ok_probe)
        report = await checker.check(env, _endpoints(env), timeout_s=10)
        assert report.passed
        assert [c.device for c in report.checks] == ["R1", "R2"]
        assert all(c.type == "ssh_reachable" and c.passed for c in report.checks)

    @pytest.mark.asyncio
    async def test_probe_failure_fails_report(self):
        env = _env()
        checker = ReadinessChecker(tcp_probe=_fail_probe, interval_s=0.01)
        report = await checker.check(env, _endpoints(env), timeout_s=0.05)
        assert not report.passed
        assert all(not c.passed for c in report.checks)
        assert report.elapsed_s > 0

    @pytest.mark.asyncio
    async def test_probe_receives_endpoint_ip_port(self):
        env = _env()
        seen: list[tuple[str, int]] = []

        async def probe(ip: str, port: int) -> bool:
            seen.append((ip, port))
            return True

        checker = ReadinessChecker(tcp_probe=probe)
        report = await checker.check(env, _endpoints(env), timeout_s=5)
        assert report.passed
        assert set(seen) == {("10.0.0.1", 2001), ("10.0.0.2", 2002)}


class TestCliMatch:
    @pytest.mark.asyncio
    async def test_cli_match_pass_and_fail(self):
        env = _env(
            [
                ReadyCheck(type="cli_match", device="R1", cli="display version", expect="Version 8"),
                ReadyCheck(type="cli_match", device="R2", cli="show version", expect="NOT_PRESENT"),
            ]
        )
        responses = {"display version": "Huawei Versatile Routing Platform, Version 8.180"}
        pool = SshPool(
            session_factory=lambda key, vendor, access: MockDeviceSession(
                key, vendor, responses=responses if vendor.netmiko_device_type == "huawei" else {}
            )
        )
        checker = ReadinessChecker(pool=pool, tcp_probe=_ok_probe)
        report = await checker.check(env, _endpoints(env), timeout_s=5)
        assert not report.passed
        by_dev = {c.device: c for c in report.checks}
        assert by_dev["R1"].passed and by_dev["R1"].type == "cli_match"
        assert not by_dev["R2"].passed

    @pytest.mark.asyncio
    async def test_cli_match_error_result_not_matched(self):
        # exec_cli ok=False (device error) must fail the check
        env = _env([ReadyCheck(type="cli_match", device="R1", cli="bad cmd", expect="x")])
        pool = SshPool(
            session_factory=lambda key, vendor, access: MockDeviceSession(key, vendor, responses={"bad cmd": "Error: unrecognized command"})
        )
        checker = ReadinessChecker(pool=pool, tcp_probe=_ok_probe)
        report = await checker.check(env, _endpoints(env), timeout_s=5)
        assert not report.passed
        assert "Error" in report.checks[0].detail or not report.checks[0].passed

    @pytest.mark.asyncio
    async def test_unknown_check_device_raises(self):
        env = _env([ReadyCheck(type="cli_match", device="RX", cli="display clock", expect="clock")])
        checker = ReadinessChecker(tcp_probe=_ok_probe)
        from rsi.core.errors import EnvError

        with pytest.raises(EnvError):
            await checker.check(env, _endpoints(env), timeout_s=5)


class TestPolling:
    @pytest.mark.asyncio
    async def test_retries_until_success(self):
        env = _env()
        calls = {"n": 0}

        async def flaky(ip: str, port: int) -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        checker = ReadinessChecker(tcp_probe=flaky, interval_s=0.01)
        report = await checker.check(env, _endpoints(env), timeout_s=5)
        assert report.passed
        assert calls["n"] >= 3  # at least three attempts happened

    @pytest.mark.asyncio
    async def test_timeout_returns_last_report(self):
        env = _env()
        checker = ReadinessChecker(tcp_probe=_fail_probe, interval_s=0.02)
        report = await checker.check(env, _endpoints(env), timeout_s=0.05)
        assert not report.passed
        assert report.elapsed_s >= 0.05
