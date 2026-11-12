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
