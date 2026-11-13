"""Unit tests: vendor SSOT + SshPool/MockDeviceSession (TU: offline P0)."""

from __future__ import annotations

import asyncio

import pytest

from rsi.core.models import SshAccess
from rsi.sandbox.sshpool import DeviceSession, ExecResult, MockDeviceSession, SessionKey, SshPool
from rsi.sandbox.vendor import VENDOR_MAP, VendorProfile


class TestVendorMap:
    def test_covers_p0_vendors(self):
        assert set(VENDOR_MAP) == {"vrp", "ce", "ios"}

    def test_ce_independent_profile_with_two_phase_commit(self):
        # D2：ce 不再别名 vrp——同命令面但独立 profile（two_phase_commit=True）
        ce, vrp = VENDOR_MAP["ce"], VENDOR_MAP["vrp"]
        assert ce is not vrp
        assert ce.netmiko_device_type == vrp.netmiko_device_type
        assert ce.enter_config == vrp.enter_config
        assert ce.reset_cmd == vrp.reset_cmd
        assert ce.two_phase_commit is True
        assert vrp.two_phase_commit is False

    def test_huawei_profile(self):
        p = VENDOR_MAP["vrp"]
        assert isinstance(p, VendorProfile)
        assert p.netmiko_device_type == "huawei"
        assert p.enter_config == "system-view"
        assert p.exit_config == "return"
        assert p.reset_cmd == ("reset saved-configuration",)
        assert p.save_config == ("save", "y")

    def test_ios_profile(self):
        p = VENDOR_MAP["ios"]
        assert p.netmiko_device_type == "cisco_ios"
        assert p.enter_config == "configure terminal"
        assert p.exit_config == "end"
        assert p.reset_cmd == ("write erase",)

    def test_error_patterns_dd04_52(self):
        p = VENDOR_MAP["vrp"]
        expected = {
            r"^Error:",
            r"^Unrecognized command",
            r"^Invalid input",
            r"^%.*\^",
            r"Wrong parameter",
        }
        assert set(p.error_patterns) == expected
        assert VENDOR_MAP["ios"].error_patterns == p.error_patterns


class TestSessionKey:
    def test_same_endpoint_same_key_across_lab_id_field(self):
        # uniqueness = (ip, port); lab_id is tracing only
        assert SessionKey("10.0.0.1", 22, "lab-a") == SessionKey("10.0.0.1", 22, "lab-a")

    def test_different_ip_different_key(self):
        assert SessionKey("10.0.0.1", 22, "lab-a") != SessionKey("10.0.0.2", 22, "lab-a")

    def test_different_port_different_key(self):
        assert SessionKey("10.0.0.1", 22, "lab-a") != SessionKey("10.0.0.1", 2222, "lab-a")

    def test_lab_id_accessibility(self):
        key = SessionKey(ip="10.0.0.1", port=22, lab_id="lab-x")
        assert key.lab_id == "lab-x"


class TestMockDeviceSession:
    def _session(self, responses=None, default=""):
        key = SessionKey("10.1.1.1", 22, "lab-1")
        return MockDeviceSession(key, VENDOR_MAP["vrp"], responses or {}, default)

    @pytest.mark.asyncio
    async def test_exec_cli_ok(self):
        s = self._session({"display version": "Huawei Versatile Routing Platform"})
        r = await s.exec_cli("display version")
        assert isinstance(r, ExecResult)
        assert r.ok
        assert r.output == "Huawei Versatile Routing Platform"
        assert r.matched_errors == []
        assert r.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_exec_cli_matches_anchored_error(self):
        s = self._session({"bad": "Error: Unrecognized command found at '^' position."})
        r = await s.exec_cli("bad")
        assert not r.ok
        assert r"^Error:" in r.matched_errors

    @pytest.mark.asyncio
    async def test_error_mid_line_not_matched(self):
        # patterns are line-anchored: embedded 'Error:' should not trip ^Error:
        s = self._session({"x": "some prefix Error: text"})
        r = await s.exec_cli("x")
        assert r.ok

    @pytest.mark.asyncio
    async def test_wrong_parameter_and_invalid_input(self):
        s = self._session(
            {
                "a": "Wrong parameter in command",
                "b": "Invalid input detected",
                "c": "% Invalid input detected at '^' marker.",
            }
        )
        assert r"Wrong parameter" in (await s.exec_cli("a")).matched_errors
        assert r"^Invalid input" in (await s.exec_cli("b")).matched_errors
        assert r"^%.*\^" in (await s.exec_cli("c")).matched_errors

    @pytest.mark.asyncio
    async def test_timed_out_forces_not_ok(self):
        s = self._session({"slow": "output with timed out marker"})
        assert not (await s.exec_cli("slow")).ok

    @pytest.mark.asyncio
    async def test_exec_config_enters_and_exits_view(self):
        s = self._session()
        r = await s.exec_config(["interface GigabitEthernet0/0/1", "ip address 10.0.0.1 24"])
        assert r.ok
        # system-view -> lines -> return
        assert s.sent[0] == "system-view"
        assert s.sent[1:3] == ["interface GigabitEthernet0/0/1", "ip address 10.0.0.1 24"]
        assert s.sent[-1] == "return"

    @pytest.mark.asyncio
    async def test_exec_config_propagates_error(self):
        s = self._session({"shutdown": "Error: incomplete command"})
        r = await s.exec_config(["shutdown"])
        assert not r.ok
        assert r"^Error:" in r.matched_errors

    @pytest.mark.asyncio
    async def test_capture_config_vrp_vs_ios(self):
        vrp = MockDeviceSession(SessionKey("1.1.1.1", 22, "l"), VENDOR_MAP["vrp"])
        ios = MockDeviceSession(SessionKey("2.2.2.2", 22, "l"), VENDOR_MAP["ios"])
        await vrp.capture_config()
        await ios.capture_config()
        assert vrp.sent == ["display current-configuration"]
        assert ios.sent == ["show running-config"]

    @pytest.mark.asyncio
    async def test_lock_serializes_commands(self):
        s = self._session()
        order: list[str] = []

        async def worker(name: str) -> None:
            async with s.lock:
                order.append(f"{name}-acquire")
                await asyncio.sleep(0.01)
                order.append(f"{name}-release")

        await asyncio.gather(worker("a"), worker("b"))
        assert order == ["a-acquire", "a-release", "b-acquire", "b-release"]


class TestSshPool:
    def _access(self, ip: str, port: int = 22) -> SshAccess:
        return SshAccess(ip=ip, port=port, username="admin", password="pw")

    @pytest.mark.asyncio
    async def test_device_creates_and_reuses(self):
        pool = SshPool()
        key = SessionKey("10.0.0.1", 22, "lab-a")
        s1 = await pool.device(key, self._access("10.0.0.1"), "vrp")
        s2 = await pool.device(key, self._access("10.0.0.1"), "vrp")
        assert s1 is s2
        assert isinstance(s1, DeviceSession)

    @pytest.mark.asyncio
    async def test_unknown_vendor_raises(self):
        pool = SshPool()
        with pytest.raises(KeyError):
            await pool.device(
                SessionKey("10.0.0.1", 22, "lab-a"), self._access("10.0.0.1"), "nxos"
            )

    @pytest.mark.asyncio
    async def test_same_endpoint_different_labs_single_session(self):
        # lab_id is not part of uniqueness
        pool = SshPool()
        s1 = await pool.device(SessionKey("10.0.0.1", 22, "lab-a"), self._access("10.0.0.1"), "vrp")
        s2 = await pool.device(SessionKey("10.0.0.1", 22, "lab-b"), self._access("10.0.0.1"), "vrp")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_close_lab_removes_only_that_lab(self):
        pool = SshPool()
        ka = SessionKey("10.0.0.1", 22, "lab-a")
        kb = SessionKey("10.0.0.2", 22, "lab-b")
        await pool.device(ka, self._access("10.0.0.1"), "vrp")
        sb = await pool.device(kb, self._access("10.0.0.2"), "vrp")
        await pool.close_lab("lab-a")
        assert (ka.ip, ka.port) not in pool._sessions
        assert pool._sessions[(kb.ip, kb.port)] is sb
        await pool.close_all()
        assert pool._sessions == {}

    @pytest.mark.asyncio
    async def test_custom_factory(self):
        made: list[SessionKey] = []

        def factory(key, profile, access=None):
            made.append(key)
            return MockDeviceSession(key, profile, {"ping 1.1.1.1": "5 packet(s) transmitted"})

        pool = SshPool(session_factory=factory)
        s = await pool.device(SessionKey("10.9.9.9", 22, "lab-z"), self._access("10.9.9.9"), "ios")
        r = await s.exec_cli("ping 1.1.1.1")
        assert r.ok and "5 packet(s)" in r.output
        assert made == [SessionKey("10.9.9.9", 22, "lab-z")]
