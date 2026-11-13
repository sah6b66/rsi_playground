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
    """记录 send_command(expect_string=...) 序列——CE6800 两阶段提交实测形态."""

    def __init__(self, *a, **kw):
        self.sent: list[tuple] = []

    def send_command(self, cli, expect_string=None, **kw):
        self.sent.append((cli, expect_string))
        if cli == "system-view":
            return "Enter system view [~Leaf-01]"
        return "[~Leaf-01]"

    def is_alive(self):
        return True

    def disconnect(self):
        pass

    def disable_paging(self, **kw):
        return ""


def test_exec_config_ce_two_phase_commit_sequence(monkeypatch):
    """E2 实测（CE6800 YunShan）：send_config_set 因 [~Leaf-01-GE1/0/1]
    （含斜杠/波浪号）提示符与两阶段提交（return 触发 Y/N/C 交互）确定性
    失败——exec_config 必须显式 expect 串行 + commit（D2：行为由
    VendorProfile.two_phase_commit 驱动，ce=True）。"""
    import asyncio

    import netmiko

    monkeypatch.setattr(netmiko, "ConnectHandler", _ConfigConn)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import netmiko_session_factory
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    s = netmiko_session_factory(
        SessionKey("1.2.3.4", 22, "l"), VENDOR_MAP["ce"],
        SshAccess(ip="1.2.3.4", username="u", password="p"),
    )
    result = asyncio.run(s.exec_config(["interface GE1/0/1", "shutdown"]))
    assert result.ok, result.matched_errors
    seq = [c for c, _ in s._conn.sent]
    assert seq == ["system-view", "interface GE1/0/1", "shutdown", "commit", "return"]
    # expect 串来自 VendorProfile 而非硬编码
    expects = [e for _, e in s._conn.sent]
    assert expects[0] == VENDOR_MAP["ce"].config_prompt_pattern
    assert expects[-1] == VENDOR_MAP["ce"].user_prompt_pattern


def test_exec_config_vrp_single_phase_no_commit(monkeypatch):
    """D2：vrp（S 系单阶段配置，立即生效）exec_config 不插 commit——
    S 系 system 视图无 commit 命令，盲发会命中错误模式。"""
    import asyncio

    import netmiko

    monkeypatch.setattr(netmiko, "ConnectHandler", _ConfigConn)
    from rsi.core.models import SshAccess
    from rsi.sandbox.netmiko_session import netmiko_session_factory
    from rsi.sandbox.sshpool import SessionKey
    from rsi.sandbox.vendor import VENDOR_MAP

    s = netmiko_session_factory(
        SessionKey("2.3.4.5", 22, "l"), VENDOR_MAP["vrp"],
        SshAccess(ip="2.3.4.5", username="u", password="p"),
    )
    result = asyncio.run(s.exec_config(["interface GE1/0/2", "undo shutdown"]))
    assert result.ok, result.matched_errors
    seq = [c for c, _ in s._conn.sent]
    assert seq == ["system-view", "interface GE1/0/2", "undo shutdown", "return"]


def test_vendor_profile_prompt_and_commit_fields():
    """D2：提示符正则/两阶段 commit 沉淀为 VendorProfile 字段（SSOT），
    供其他 VRP 型号复用——不再散落 netmiko_session 硬编码。"""
    import re

    ce, vrp = VENDOR_MAP["ce"], VENDOR_MAP["vrp"]
    assert ce.two_phase_commit is True
    assert vrp.two_phase_commit is False
    # CE/VRP8 配置提示符含 ~（未提交态）与 /（接口子视图）；commit 后 ~ 消失
    for prompt in ("[~Leaf-01]", "[~Leaf-01-GE1/0/1]", "[Leaf-01]"):
        assert re.search(ce.config_prompt_pattern, prompt), prompt
    assert re.search(vrp.config_prompt_pattern, "[Leaf-01]")
    assert re.search(ce.user_prompt_pattern, "<Leaf-01>")
    assert ce.enter_config == "system-view" and ce.exit_config == "return"


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
