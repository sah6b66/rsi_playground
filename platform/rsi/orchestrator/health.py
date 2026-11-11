"""HealthMonitor — 8-class external dependency monitor (DD-12 §4.7, D-1).

Runs registered checkers (docker/lab/instances/llm/git/db/collector/
evalset_api/network_prep), caches the latest results for ``GET /system/dependencies`` and
(optionally) polls them on an interval in a background asyncio task,
publishing ``system.alert`` events on up<->down state transitions.

Every checker must be offline-safe: it never raises — failures and
"component not configured" both land in the HealthResult.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rsi.log import get_logger

__all__ = ["HealthMonitor", "HealthResult", "build_checkers"]

logger = get_logger(__name__)


class HealthResult:
    """One checker outcome. ``ok is None`` means "not configured/unknown"."""

    __slots__ = ("name", "ok", "detail", "checked_at", "latency_ms")

    def __init__(
        self,
        name: str,
        ok: bool | None,
        detail: str = "",
        checked_at: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        self.name = name
        self.ok = ok
        self.detail = detail
        self.checked_at = checked_at or datetime.now(UTC).isoformat(
            timespec="milliseconds"
        )
        self.latency_ms = latency_ms

    @property
    def status(self) -> str:
        if self.ok is True:
            return "up"
        if self.ok is False:
            return "down"
        return "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
        }


Checker = Callable[[], HealthResult]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class HealthMonitor:
    """Checker registry + optional 30s polling loop (DD-12 §4.7)."""

    def __init__(
        self,
        checkers: dict[str, Checker],
        interval_s: float = 30.0,
        event_bus: Any = None,
    ) -> None:
        self.checkers = dict(checkers)
        self.interval_s = float(interval_s)
        self.event_bus = event_bus
        self._cached: dict[str, HealthResult] = {}
        self._task: asyncio.Task | None = None
        self._previous_status: dict[str, str] = {}

    # -- synchronous full sweep ------------------------------------------

    def check_all(self) -> dict[str, HealthResult]:
        """Run every checker; exceptions degrade to ok=False, never raise."""
        results: dict[str, HealthResult] = {}
        for name, checker in self.checkers.items():
            started = time.monotonic()
            try:
                result = checker()
            except Exception as exc:  # noqa: BLE001 - offline safety
                logger.warning(
                    "health.checker_failed", checker=name, exc_type=type(exc).__name__
                )
                result = HealthResult(
                    name=name, ok=False, detail=f"checker error: {type(exc).__name__}"
                )
            if result.latency_ms is None:
                result.latency_ms = round((time.monotonic() - started) * 1000, 2)
            results[name] = result
        self._cached = results
        self._alert_transitions(results)
        return results

    def get_cached(self) -> list[dict[str, Any]]:
        """Latest results in /system/dependencies item shape (may be empty
        before the first sweep)."""
        return [r.as_dict() for r in self._cached.values()]

    # -- background polling ----------------------------------------------

    async def start(self) -> asyncio.Task | None:
        """Start the interval polling task (idempotent)."""
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._poll_loop(), name="health-monitor")
        return self._task

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        while True:
            try:
                # checkers are cheap sync probes run on the loop thread so
                # transition alerts (create_task) can be scheduled
                self.check_all()
                await asyncio.sleep(0)  # let scheduled alert tasks run
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - poller must not die
                logger.warning(
                    "health.poll_failed", exc_type=type(exc).__name__
                )
            await asyncio.sleep(self.interval_s)

    def _alert_transitions(self, results: dict[str, HealthResult]) -> None:
        """Publish system.alert on up<->down transitions (H2 SSE)."""
        if self.event_bus is None:
            return
        publish_alert = getattr(self.event_bus, "publish_alert", None)
        if publish_alert is None:
            return
        for name, result in results.items():
            prev = self._previous_status.get(name)
            status = result.status
            if prev is not None and prev != status:
                try:
                    coro = publish_alert(
                        "warning" if status != "up" else "info",
                        f"依赖 {name} 状态翻转: {prev} -> {status} ({result.detail})",
                        scope=f"health.{name}",
                    )
                    # fire-and-forget safe call from sync context
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop is not None:
                        loop.create_task(coro)
                except Exception:  # noqa: BLE001 - alerts never break checks
                    pass
            self._previous_status[name] = status


# ---------------------------------------------------------------------------
# Checker factory: services dict (rsi.bootstrap.build_services shape) ->
# the 8 DD-12 §4.7 checker families. All offline-safe.
# ---------------------------------------------------------------------------


def _not_configured(name: str) -> HealthResult:
    return HealthResult(name=name, ok=None, detail="not_configured")


def _mk_docker(services: dict) -> Checker:
    host_pool = services.get("host_pool")

    def check() -> HealthResult:
        if host_pool is None:
            return _not_configured("docker")
        try:
            hosts = getattr(host_pool, "hosts", None) or getattr(
                host_pool, "_configs", {}
            )
            names = list(hosts) if hosts else []
            if not names:
                return HealthResult(
                    "docker", ok=True, detail="offline stub pool (no hosts)"
                )
            reachable = [h for h in names if host_pool.check_reach(h)]
            ok = len(reachable) == len(names)
            detail = f"{len(reachable)}/{len(names)} hosts reachable"
            return HealthResult("docker", ok=ok, detail=detail)
        except Exception as exc:  # noqa: BLE001
            return HealthResult("docker", ok=False, detail=type(exc).__name__)

    return check


def _mk_lab(services: dict) -> Checker:
    sandbox = services.get("sandbox_service")

    def check() -> HealthResult:
        if sandbox is None:
            return _not_configured("lab")
        try:
            labs = getattr(sandbox, "labs", None)
            if not labs:
                return HealthResult(
                    "lab", ok=True, detail="0 active labs"
                )
            return HealthResult(
                "lab", ok=True, detail=f"{len(labs)} active labs"
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult("lab", ok=False, detail=type(exc).__name__)

    return check


def _mk_instances(services: dict) -> Checker:
    carrier = services.get("host_service")

    def check() -> HealthResult:
        if carrier is None:
            return _not_configured("instances")
        try:
            active = getattr(carrier, "active_instances", None)
            if active is None:
                return _not_configured("instances")
            n = active() if callable(active) else len(active)
            return HealthResult(
                "instances", ok=True, detail=f"{n} running instances"
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult("instances", ok=False, detail=type(exc).__name__)

    return check


def _mk_llm(services: dict) -> Checker:
    llm_client = services.get("llm_client")

    def check() -> HealthResult:
        if llm_client is None:
            return _not_configured("llm")
        handler = getattr(llm_client, "handler", None)
        name = getattr(handler, "__name__", "") or getattr(
            handler, "__qualname__", ""
        )
        if "offline" in str(name).lower() or "mock" in str(name).lower():
            return HealthResult("llm", ok=None, detail="offline mock handler")
        return HealthResult("llm", ok=True, detail=str(name) or "configured")

    return check


def _mk_git(services: dict) -> Checker:
    git_store = services.get("git_store")

    def check() -> HealthResult:
        if git_store is None:
            return _not_configured("git")
        repo = _repo_path(git_store)
        if repo is None:
            return _not_configured("git")
        try:
            parent_ok = repo.parent.exists() and _writable(repo.parent)
            return HealthResult(
                "git", ok=parent_ok,
                detail=str(repo) + ("" if parent_ok else " (not writable)"),
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult("git", ok=False, detail=type(exc).__name__)

    return check


def _repo_path(git_store: Any):
    repo = getattr(git_store, "repo_path", None)
    return repo


def _writable(path) -> bool:
    import os
    import tempfile

    try:
        fd, tmp = tempfile.mkstemp(dir=str(path), prefix=".health-")
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


def _mk_db(services: dict) -> Checker:
    session_factory = services.get("session_factory")

    def check() -> HealthResult:
        if session_factory is None:
            return _not_configured("db")
        try:
            from sqlalchemy import text

            with session_factory() as session:
                session.exec(text("SELECT 1"))  # type: ignore[union-attr]
            return HealthResult("db", ok=True, detail="SELECT 1 ok")
        except Exception as exc:  # noqa: BLE001
            return HealthResult("db", ok=False, detail=type(exc).__name__)

    return check


def _mk_collector(services: dict) -> Checker:
    collector = services.get("tracing_collector")

    def check() -> HealthResult:
        if collector is None:
            return _not_configured("collector")
        try:
            buffered = getattr(collector, "_events", None)
            depth = len(buffered) if isinstance(buffered, dict) else 0
            ok = depth <= 1000  # degraded above (DD §4.7 backlog rule)
            return HealthResult(
                "collector",
                ok=ok,
                detail=f"buffered_runs={depth}",
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResult("collector", ok=False, detail=type(exc).__name__)

    return check


def _probe_url(name: str, base_url: str) -> HealthResult:
    """Liveness probe for a system-level HTTP API (ADR-27 D5).

    GET ``{url}/health`` first, fall back to ``{url}/``; any HTTP response
    counts as reachable (ok=True), transport errors count as down.
    """
    import httpx

    url = (base_url or "").rstrip("/")
    for path in ("/health", "/"):
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{url}{path}")
        except Exception as exc:  # noqa: BLE001 - offline safety
            return HealthResult(name, ok=False, detail=type(exc).__name__)
        # /health non-2xx → try the root; any response from "/" still
        # proves the service is up
        if resp.status_code < 400 or path == "/":
            return HealthResult(
                name, ok=True, detail=f"HTTP {resp.status_code} ({path})"
            )
    return HealthResult(name, ok=False, detail="unreachable")  # pragma: no cover
    return HealthResult(name, ok=False, detail="unreachable")  # pragma: no cover


def _mk_evalset_api(services: dict) -> Checker:
    """D5: evalset_api — probe ``settings.evalset_api.url`` (ADR-27)."""
    fetcher = services.get("evalset_fetcher")
    base_url = (getattr(fetcher, "base_url", "") or "") if fetcher else ""

    def check() -> HealthResult:
        if not base_url:
            return _not_configured("evalset_api")
        return _probe_url("evalset_api", base_url)

    return check


def _mk_network_prep(services: dict) -> Checker:
    """D5: network_prep — probe ``settings.network_prep_api.url`` (ADR-27)."""
    netprep = services.get("netprep")
    base_url = (getattr(netprep, "base_url", "") or "") if netprep else ""

    def check() -> HealthResult:
        if not base_url:
            return _not_configured("network_prep")
        return _probe_url("network_prep", base_url)

    return check


def _mk_platform_checkers(services: dict) -> dict[str, Checker]:
    """T4: one checker per platform backend probe (``platform:<key>``).

    The probes dict comes from bootstrap (``services["platform_probes"]``);
    when no platform backends are configured a single ``platform`` checker
    reports not_configured. Probe results are TTL-cached, so sweeps are
    cheap. Offline-safe: probe failures land in the result, never raise.
    """
    probes = services.get("platform_probes") or {}
    if not probes:
        def check_missing() -> HealthResult:
            return _not_configured("platform")

        return {"platform": check_missing}

    checkers: dict[str, Checker] = {}
    for key, probe in probes.items():
        def check(probe=probe, key=key) -> HealthResult:
            name = f"platform:{key}"
            try:
                result = probe.probe_cached()
                return HealthResult(
                    name,
                    ok=result.ok,
                    detail=result.detail,
                    latency_ms=result.latency_ms,
                )
            except Exception as exc:  # noqa: BLE001 - offline safety
                return HealthResult(name, ok=False, detail=type(exc).__name__)

        checkers[f"platform:{key}"] = check
    return checkers


def build_checkers(services: dict[str, Any]) -> dict[str, Checker]:
    """Build the DD-12 §4.7 checker families (+T4 ``platform:<key>``) from a
    bootstrap services dict."""
    checkers = {
        "docker": _mk_docker(services),
        "lab": _mk_lab(services),
        "instances": _mk_instances(services),
        "llm": _mk_llm(services),
        "git": _mk_git(services),
        "db": _mk_db(services),
        "collector": _mk_collector(services),
        "evalset_api": _mk_evalset_api(services),
        "network_prep": _mk_network_prep(services),
    }
    checkers.update(_mk_platform_checkers(services))
    return checkers
