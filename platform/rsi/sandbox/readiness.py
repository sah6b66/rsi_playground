"""ReadinessChecker (DD-04 §4 wait_ready, §5) — poll env until ready.

``ssh_reachable`` probes a TCP connect to ``(ip, port)`` with a 3s timeout;
``cli_match`` runs a CLI command via a pooled ``DeviceSession`` and checks
that the ``expect`` string appears in the output. When ``env.ready_checks``
is empty, the default is one ``ssh_reachable`` check per device.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from rsi.core.errors import EnvError
from rsi.log import get_logger
from rsi.core.models import EnvSpec, ReadyCheck, SshAccess
from rsi.sandbox.sshpool import SessionKey, SshPool

__all__ = ["CheckResult", "ReadinessChecker", "ReadyReport"]

TcpProbe = Callable[[str, int], Awaitable[bool]]

PROBE_TIMEOUT_S = 3.0
POLL_INTERVAL_S = 5.0

logger = get_logger(__name__)


class CheckResult(BaseModel):
    type: str
    device: str
    passed: bool
    detail: str = ""


class ReadyReport(BaseModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    elapsed_s: float = 0.0

    def summary(self) -> dict[str, Any]:
        return self.model_dump()


async def default_tcp_probe(ip: str, port: int) -> bool:
    """TCP connect probe with a short timeout (DD-04 ssh_reachable)."""
    try:
        writer = None
        fut = asyncio.open_connection(ip, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=PROBE_TIMEOUT_S)
        return True
    except (TimeoutError, OSError):
        return False
    finally:
        if writer is not None:
            writer.close()


class ReadinessChecker:
    """Evaluate env.ready_checks, retrying every ``interval_s`` until ``timeout_s``."""

    def __init__(
        self,
        pool: SshPool | None = None,
        tcp_probe: TcpProbe | None = None,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._pool = pool if pool is not None else SshPool()
        self._tcp_probe = tcp_probe or default_tcp_probe
        self._interval_s = interval_s

    async def check(
        self,
        env: EnvSpec,
        endpoints: dict[str, SshAccess],
        timeout_s: int = 600,
    ) -> ReadyReport:
        logger.debug(
            "check.entry",
            env=env.name,
            devices=len(endpoints),
            timeout_s=timeout_s,
        )
        checks = self._resolve_checks(env)
        deadline = time.monotonic() + timeout_s
        start = time.monotonic()
        report = ReadyReport(passed=False)
        while True:
            report = await self._run_checks(env, endpoints, checks)
            report.elapsed_s = time.monotonic() - start
            if report.passed:
                logger.debug("check.exit", passed=True, elapsed_s=round(report.elapsed_s, 3))
                return report
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.debug(
                    "check.exit", passed=False, elapsed_s=round(report.elapsed_s, 3), reason="timeout"
                )
                return report
            logger.debug("check.poll_retry", remaining_s=round(remaining, 3))
            await asyncio.sleep(min(self._interval_s, remaining))

    # -- internals ----------------------------------------------------------

    def _resolve_checks(self, env: EnvSpec) -> list[ReadyCheck]:
        if env.ready_checks:
            return list(env.ready_checks)
        return [
            ReadyCheck(type="ssh_reachable", device=name)
            for name in sorted(env.ssh_access)
        ]

    async def _run_checks(
        self,
        env: EnvSpec,
        endpoints: dict[str, SshAccess],
        checks: list[ReadyCheck],
    ) -> ReadyReport:
        results = await asyncio.gather(
            *(self._run_one(env, endpoints, c) for c in checks)
        )
        return ReadyReport(passed=all(r.passed for r in results), checks=results)

    async def _run_one(
        self,
        env: EnvSpec,
        endpoints: dict[str, SshAccess],
        check: ReadyCheck,
    ) -> CheckResult:
        device = check.device
        if device is not None and device not in endpoints:
            raise EnvError(
                f"readiness check references unknown device {device!r}",
                context={"device": device, "env": env.name},
            )
        if check.type == "ssh_reachable":
            if device is None:  # pragma: no cover - model forbids in practice
                raise EnvError("ssh_reachable check requires a device")
            access = endpoints[device]
            ok = await self._tcp_probe(access.ip, access.port)
            return CheckResult(
                type=check.type,
                device=device,
                passed=ok,
                detail=f"tcp {access.ip}:{access.port}"
                + ("" if ok else " unreachable"),
            )
        # cli_match
        assert device is not None and check.cli and check.expect
        access = endpoints[device]
        vendor = next(d.type for d in env.devices if d.name == device)
        key = SessionKey(ip=access.ip, port=access.port, lab_id=env.name)
        session = await self._pool.device(key, access, vendor)
        result = await session.exec_cli(check.cli)
        if not result.ok:
            return CheckResult(
                type=check.type,
                device=device,
                passed=False,
                detail=f"cli error: {result.output[:200]!r} matched={result.matched_errors}",
            )
        passed = check.expect in result.output
        return CheckResult(
            type=check.type,
            device=device,
            passed=passed,
            detail=""
            if passed
            else f"{check.expect!r} not found in output: {result.output[:200]!r}",
        )
