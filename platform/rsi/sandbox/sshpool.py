"""Device SSH session pool (DD-04 §4.2/§5.1) — P0 offline mock.

P0 has no real netmiko/SSH: ``DeviceSession`` is abstract with the concrete
``MockDeviceSession`` returning canned responses, so downstream services
(SandboxService, judge) can be developed and tested fully offline. The real
netmiko-backed session lands in the S11 slice.
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from typing import NamedTuple

from pydantic import BaseModel, Field

from rsi.core.models import SshAccess
from rsi.log import get_logger
from rsi.sandbox.vendor import VENDOR_MAP, VendorProfile

__all__ = [
    "DeviceSession",
    "ExecResult",
    "MockDeviceSession",
    "SessionKey",
    "SshPool",
]

logger = get_logger(__name__)


class SessionKey(NamedTuple):
    """Pool key (ADR-24 v1.1): (ip, port) is globally unique across labs.

    ``lab_id`` is carried for ownership tracing and batch cleanup on lab
    teardown, but does not participate in uniqueness — the same device name
    in different labs resolves to different (ip, port) endpoints.
    """

    ip: str
    port: int
    lab_id: str


class ExecResult(BaseModel):
    """SSH transport success and command success are separate layers."""

    ok: bool
    output: str = ""
    matched_errors: list[str] = Field(default_factory=list)
    duration_ms: int = 0


def match_errors(output: str, vendor: VendorProfile) -> list[str]:
    """Return the vendor error patterns anchored-matched by any output line."""
    matched: list[str] = []
    for pattern in vendor.error_patterns:
        rx = re.compile(pattern)
        if any(rx.match(line.strip()) for line in output.splitlines()):
            matched.append(pattern)
    return matched


class DeviceSession(ABC):
    """One SSH session to one device; commands serialized via lock."""

    def __init__(self, key: SessionKey, vendor: VendorProfile) -> None:
        self.key = key
        self.vendor = vendor
        self.lock = asyncio.Lock()

    async def exec_cli(self, cli: str, read_timeout_s: int = 120) -> ExecResult:
        """Execute one CLI command and judge per-line against error patterns."""
        async with self.lock:
            start = time.perf_counter()
            output = await self._send(cli, read_timeout_s)
            duration_ms = int((time.perf_counter() - start) * 1000)
            matched = match_errors(output, self.vendor)
            ok = not matched and "timed out" not in output
            return ExecResult(
                ok=ok, output=output, matched_errors=matched, duration_ms=duration_ms
            )

    async def exec_config(self, lines: list[str]) -> ExecResult:
        """Enter config view -> run lines -> exit back to user view."""
        async with self.lock:
            start = time.perf_counter()
            output_parts: list[str] = []
            output_parts.append(await self._send(self.vendor.enter_config, 120))
            for line in lines:
                output_parts.append(await self._send(line, 120))
            output_parts.append(await self._send(self.vendor.exit_config, 120))
            output = "\n".join(output_parts)
            duration_ms = int((time.perf_counter() - start) * 1000)
            matched = match_errors(output, self.vendor)
            ok = not matched and "timed out" not in output
            return ExecResult(
                ok=ok, output=output, matched_errors=matched, duration_ms=duration_ms
            )

    async def capture_config(self) -> str:
        """Full running configuration text."""
        result = await self.exec_cli(self._capture_cmd)
        return result.output

    async def close(self) -> None:
        await self._close()

    def is_alive(self) -> bool:
        """Liveness probe (B-4, DD §4.2). Base implementation assumes the
        session is healthy; real transports override with transport-level
        keepalive/flag checks."""
        return True

    @property
    def _capture_cmd(self) -> str:
        if self.vendor.netmiko_device_type == "huawei":
            return "display current-configuration"
        return "show running-config"

    @abstractmethod
    async def _send(self, cli: str, read_timeout_s: int) -> str: ...

    @abstractmethod
    async def _close(self) -> None: ...


class MockDeviceSession(DeviceSession):
    """Offline mock: command -> canned output dict; unknown -> default."""

    def __init__(
        self,
        key: SessionKey,
        vendor: VendorProfile,
        responses: dict[str, str] | None = None,
        default_response: str = "",
    ) -> None:
        super().__init__(key, vendor)
        self.responses = responses or {}
        self.default_response = default_response
        self.sent: list[str] = []  # every command seen, for assertions

    async def _send(self, cli: str, read_timeout_s: int) -> str:
        self.sent.append(cli)
        return self.responses.get(cli, self.default_response)

    async def _close(self) -> None:
        return None


class SshPool:
    """Get-or-create pool of DeviceSessions keyed by (ip, port, lab_id)."""

    def __init__(self, session_factory=None) -> None:
        # P0: injectable factory keeps the pool offline-mockable; the S11
        # slice swaps in a netmiko ConnectHandler-backed factory.
        # internal key = (ip, port): lab_id must not split sessions for the
        # same endpoint (ADR-24; SessionKey.lab_id is tracing only)
        self._sessions: dict[tuple[str, int], DeviceSession] = {}
        self._factory = session_factory

    async def device(
        self, key: SessionKey, access: SshAccess, vendor: str
    ) -> DeviceSession:
        """Reuse the live session for this endpoint or create a new one.

        B-4 (DD §4.2): a pooled hit is only reused while ``is_alive()``;
        a dead session is closed and replaced with a fresh one.
        """
        existing = self._sessions.get((key.ip, key.port))
        if existing is not None and existing.is_alive():
            return existing
        if existing is not None:
            logger.debug("ssh_pool.session_dead", ip=key.ip, port=key.port)
            del self._sessions[(key.ip, key.port)]
            try:
                await existing.close()
            except Exception:  # noqa: BLE001 - dead session close is best-effort
                pass
        profile = VENDOR_MAP[vendor]
        session = (
            self._factory(key, profile)
            if self._factory is not None
            else MockDeviceSession(key, profile)
        )
        self._sessions[(key.ip, key.port)] = session
        return session

    async def close_lab(self, lab_id: str) -> None:
        """Batch-reclaim all sessions belonging to a lab (lab destroy)."""
        doomed = [
            (k, s) for k, s in self._sessions.items() if s.key.lab_id == lab_id
        ]
        for k, session in doomed:
            del self._sessions[k]
            await session.close()

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session.close()
