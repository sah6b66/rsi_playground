"""NetmikoDeviceSession（A3）：真实 SSH 会话离线单测."""

import asyncio
import sys
import types

from rsi.core.models import SshAccess
from rsi.sandbox.netmiko_session import (
    NetmikoDeviceSession,
    netmiko_session_factory,
)
from rsi.sandbox.sshpool import SessionKey, SshPool
from rsi.sandbox.vendor import VENDOR_MAP


class _FakeConn:
    def __init__(self, *a, **kw):
        self.kw = kw
        self.sent: list[str] = []
        self.alive = True

    def send_command(self, cli, **kw):
        self.sent.append(cli)
        return f"OUT<{cli}>"

    def is_alive(self):
        return self.alive

    def disconnect(self):
        self.alive = False


def _patch_netmiko(monkeypatch, cls=_FakeConn):
    mod = types.ModuleType("netmiko")
    mod.ConnectHandler = cls
    monkeypatch.setitem(sys.modules, "netmiko", mod)


def test_send_via_netmiko(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(
        ip="192.168.96.11", port=22, username="CLI_Agen", password="Adm@1234"
    )
    s = netmiko_session_factory(
        SessionKey(ip=access.ip, port=access.port, lab_id="lab-x"),
        VENDOR_MAP["vrp"],
        access,
    )
    out = asyncio.run(s.exec_cli("display version"))
    assert out.ok and out.output == "OUT<display version>"


def test_connect_uses_access_credentials(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(ip="192.168.96.21", port=2222, username="u", password="p")
    s = NetmikoDeviceSession(
        SessionKey(access.ip, 2222, "lab"), VENDOR_MAP["vrp"], access
    )
    asyncio.run(s.exec_cli("display clock"))
    assert s._conn.kw["host"] == "192.168.96.21"
    assert s._conn.kw["port"] == 2222
    assert s._conn.kw["username"] == "u"
    assert s._conn.kw["device_type"] == "huawei"


def test_close_and_liveness(monkeypatch):
    _patch_netmiko(monkeypatch)
    access = SshAccess(ip="1.2.3.4", username="u", password="p")
    s = NetmikoDeviceSession(
        SessionKey("1.2.3.4", 22, "l"), VENDOR_MAP["vrp"], access
    )
    assert s.is_alive() is False  # 未连接
    asyncio.run(s.exec_cli("display version"))
    assert s.is_alive() is True
    conn = s._conn
    asyncio.run(s.close())
    assert s._conn is None
    assert conn.alive is False


def test_sshpool_factory_receives_access(monkeypatch):
    _patch_netmiko(monkeypatch)
    pool = SshPool(session_factory=netmiko_session_factory)
    access = SshAccess(ip="192.168.96.12", username="u", password="p")
    session = asyncio.run(
        pool.device(SessionKey(access.ip, 22, "lab-1"), access, "vrp")
    )
    assert isinstance(session, NetmikoDeviceSession)


class _FlakyConn:
    """前 N 次 connect 被 RST（eNSP banner 10054 实测形态），之后成功."""

    calls = 0

    def __init__(self, *a, **kw):
        type(self).calls += 1
        if type(self).calls <= 1:
            import paramiko

            raise paramiko.ssh_exception.SSHException(
                "Error reading SSH protocol banner"
            )
        self.sent = []

    def send_command(self, cli, **kw):
        self.sent.append(cli)
        return f"OK<{cli}>"

    def is_alive(self):
        return True

    def disconnect(self):
        pass


def test_connect_retries_on_banner_reset(monkeypatch):
    """E2 实测：eNSP SSH 对快速重连 RST——_connect 必须带退避重试."""
    import asyncio
    import types

    import netmiko  # real dep since A3

    monkeypatch.setattr(netmiko, "ConnectHandler", _FlakyConn)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import NetmikoDeviceSession, _RETRY_DELAYS
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    s = NetmikoDeviceSession(
        SessionKey("192.168.96.11", 22, "lab"), VENDOR_MAP["vrp"],
        SshAccess(ip="192.168.96.11", username="CLI_Agent", password="x"),
    )
    monkeypatch.setattr("rsi.sandbox.netmiko_session._sleep", lambda s: None)
    out = asyncio.run(s.exec_cli("display version"))
    assert out.ok and out.output == "OK<display version>"
    assert _FlakyConn.calls == 2  # 第一次 RST，重试成功
    assert len(_RETRY_DELAYS) >= 2


class _ConfigConn:
    def __init__(self, *a, **kw):
        self.config_batches = []
        self.commands = []

    def send_command(self, cli, **kw):
        self.commands.append(cli)
        return f"OUT<{cli}>"

    def send_config_set(self, lines, **kw):
        self.config_batches.append(list(lines))
        return "[config] " + " | ".join(lines)

    def is_alive(self):
        return True

    def disconnect(self):
        pass


def test_exec_config_uses_send_config_set(monkeypatch):
    """E2 实测：exec_cli 逐行下发配置命令不进系统视图必失败——exec_config
    必须经 netmiko send_config_set 成批下发。"""
    import asyncio

    import netmiko

    monkeypatch.setattr(netmiko, "ConnectHandler", _ConfigConn)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import netmiko_session_factory
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    s = netmiko_session_factory(
        SessionKey("1.2.3.4", 22, "l"), VENDOR_MAP["vrp"],
        SshAccess(ip="1.2.3.4", username="u", password="p"),
    )
    result = asyncio.run(s.exec_config(["interface GE1/0/1", "shutdown"]))
    assert result.ok
    assert s._conn.config_batches == [["interface GE1/0/1", "shutdown"]]


class _PagingConn(_ConfigConn):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.paging_disabled = False

    def disable_paging(self, **kw):
        self.paging_disabled = True
        return "Info: The configuration of the terminal has been changed."


def test_connect_disables_paging(monkeypatch):
    """E2 实测：display current-configuration 全量输出被 More 分页卡死
    （prompt '>' 不再出现 → ReadTimeout）——建连后必须显式关分页。"""
    import asyncio

    import netmiko

    monkeypatch.setattr(netmiko, "ConnectHandler", _PagingConn)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import netmiko_session_factory
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    s = netmiko_session_factory(
        SessionKey("5.6.7.8", 22, "l2"), VENDOR_MAP["vrp"],
        SshAccess(ip="5.6.7.8", username="u", password="p"),
    )
    asyncio.run(s.exec_cli("display version"))
    assert s._conn.paging_disabled is True


class _StallConn:
    """首次 send_command 卡死（ReadTimeout 形态），重连后成功."""

    calls = 0

    def __init__(self, *a, **kw):
        self.commands = []

    def send_command(self, cli, **kw):
        from netmiko.exceptions import ReadTimeout

        _StallConn.calls += 1
        if _StallConn.calls == 1:
            raise ReadTimeout("Pattern not detected: '>' in output.")
        return f"OK<{cli}>"

    def disable_paging(self, **kw):
        return ""

    def is_alive(self):
        return True

    def disconnect(self):
        pass


def test_send_retries_once_after_stall(monkeypatch):
    """E2 实测：agent 容器大量 SSH 后设备 VTY 瞬时劣化——首读超时须
    重连重试一次。"""
    import asyncio

    import netmiko

    monkeypatch.setattr(netmiko, "ConnectHandler", _StallConn)
    monkeypatch.setattr("rsi.sandbox.netmiko_session._sleep", lambda s: None)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import netmiko_session_factory
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    _StallConn.calls = 0
    s = netmiko_session_factory(
        SessionKey("9.9.9.9", 22, "l3"), VENDOR_MAP["vrp"],
        SshAccess(ip="9.9.9.9", username="u", password="p"),
    )
    out = asyncio.run(s.exec_cli("display version"))
    assert out.ok and out.output == "OK<display version>"
    assert _StallConn.calls == 2
