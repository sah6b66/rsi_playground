"""netmiko 真实设备会话（S11，DD-04 §4.2 真实 SSH 切片）.

延迟连接、命令经 ``asyncio.to_thread`` 执行（H1：DeviceSession.lock
绑定创建它的循环，所有阻塞 IO 必须离开事件循环）。
"""

from __future__ import annotations

import asyncio

from rsi.core.models import SshAccess
from rsi.sandbox.sshpool import DeviceSession, SessionKey
from rsi.sandbox.vendor import VendorProfile

__all__ = ["NetmikoDeviceSession", "netmiko_session_factory"]


class NetmikoDeviceSession(DeviceSession):
    """One real SSH session to one device via netmiko ConnectHandler."""

    def __init__(
        self, key: SessionKey, vendor: VendorProfile, access: SshAccess
    ) -> None:
        super().__init__(key, vendor)
        self._access = access
        self._conn = None

    def _connect(self):
        if self._conn is not None and self._conn.is_alive():
            return self._conn
        from netmiko import ConnectHandler  # 延迟导入：离线环境可运行

        self._conn = ConnectHandler(
            device_type=self.vendor.netmiko_device_type,
            host=self._access.ip,
            port=self._access.port,
            username=self._access.username,
            password=self._access.password or "",
            conn_timeout=15,
            banner_timeout=30,
        )
        return self._conn

    def _send_sync(self, cli: str, read_timeout_s: int) -> str:
        conn = self._connect()
        return str(conn.send_command(cli, read_timeout=max(read_timeout_s, 10)))

    async def _send(self, cli: str, read_timeout_s: int) -> str:
        return await asyncio.to_thread(self._send_sync, cli, read_timeout_s)

    def is_alive(self) -> bool:
        try:
            return bool(self._conn is not None and self._conn.is_alive())
        except Exception:  # noqa: BLE001 - 传输层异常按死链处理
            return False

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.disconnect)


def netmiko_session_factory(
    key: SessionKey, profile: VendorProfile, access: SshAccess
) -> NetmikoDeviceSession:
    """SshPool session_factory 协议：三参（key, vendor, ssh_access）."""
    return NetmikoDeviceSession(key, profile, access)
