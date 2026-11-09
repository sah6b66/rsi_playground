"""D-1 HealthMonitor + /system/dependencies + D-5 STOP endpoints (F4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rsi.api.app import create_app
from rsi.orchestrator.budget import BudgetController
from rsi.orchestrator.event_bus import EventBus
from rsi.orchestrator.health import (
    HealthMonitor,
    HealthResult,
    build_checkers,
)
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

pytestmark = pytest.mark.asyncio


def _ok(name: str = "x"):
    return HealthResult(name=name, ok=True, detail="fine")


def _boom(_name: str = "y"):
    raise RuntimeError("probe exploded")


def make_services(**extra) -> dict:
    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "admin": {
                "user_id": "u-admin",
                "role": "admin",
                "password_hash": auth.hash_password("admin-pw"),
            },
            "researcher": {
                "user_id": "u-res",
                "role": "researcher",
                "password_hash": auth.hash_password("res-pw"),
            },
        },
    }
    services.update(extra)
    return services


async def _login(client, user: str, pw: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": user, "password": pw}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestHealthMonitorUnit:
    async def test_check_all_all_ok(self):
        mon = HealthMonitor({"docker": _ok, "db": lambda: _ok("db")})
        results = mon.check_all()
        assert set(results) == {"docker", "db"}
        assert all(r.ok is True for r in results.values())
        assert all(r.checked_at for r in results.values())

    async def test_raising_checker_degrades_not_explodes(self):
        mon = HealthMonitor({"docker": _ok, "flaky": _boom})
        results = mon.check_all()
        assert results["docker"].ok is True
        assert results["flaky"].ok is False
        assert "RuntimeError" in results["flaky"].detail

    async def test_start_stop_polling_and_alerts(self):
        bus = EventBus()
        q = await bus.subscribe()
        calls = {"n": 0}

        def checker() -> HealthResult:
            calls["n"] += 1
            # up on first sweep, down afterwards -> transition alert
            return HealthResult("db", ok=calls["n"] <= 1, detail="t")

        mon = HealthMonitor({"db": checker}, interval_s=0.05, event_bus=bus)
        await mon.start()
        await asyncio.sleep(0.25)
        await mon.stop()
        assert calls["n"] >= 3  # polling actually happened
        alerts = []
        while not q.empty():
            alerts.append(q.get_nowait())
        assert any(a["event"] == "system.alert" for a in alerts)

    async def test_build_checkers_offline_safe(self):
        # empty services -> all 8 checkers + platform not_configured, none raise
        checkers = build_checkers({})
        assert set(checkers) == {
            "docker", "lab", "instances", "llm", "git", "db", "collector",
            "config_api", "platform",
        }
        mon = HealthMonitor(checkers)
        results = mon.check_all()
        assert len(results) == 9

    async def test_build_checkers_on_bootstrap_services(self):
        import time

        from rsi.bootstrap import build_services
        from rsi.sandbox.platform_probe import PlatformProbeResult

        services = build_services()
        # default settings carry placeholder platform URLs — seed each
        # probe's TTL cache so the sweep stays offline and deterministic
        for probe in services.get("platform_probes", {}).values():
            probe._cached = PlatformProbeResult(ok=True, detail="HTTP 200")
            probe._cached_at = time.monotonic()
        mon = services["health_monitor"]
        results = mon.check_all()
        assert set(results) == {
            "docker", "lab", "instances", "llm", "git", "db", "collector",
            "config_api",
        } | {f"platform:{k}" for k in services["platform_probes"]}
        # offline assembly: stub pool is up, llm detected as the offline mock
        assert results["docker"].ok is True
        assert results["llm"].status in ("unknown", "up")
        assert all(
            results[f"platform:{k}"].ok is True for k in services["platform_probes"]
        )


class TestDependenciesEndpoint:
    @pytest_asyncio.fixture
    async def client_factory(self):
        async def _make(services):
            app = create_app(services)
            return AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )

        return _make

    async def test_no_monitor_marks_fallback(self, client_factory):
        async with await client_factory(make_services()) as client:
            h = await _login(client, "researcher", "res-pw")
            resp = await client.get(
                "/api/v1/system/dependencies", headers=h
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["source"] == "fallback"
            assert {i["name"] for i in body["items"]} == {
                "git-store", "llm-gateway", "sandbox", "db",
            }

    async def test_monitor_returns_real_checkers(self, client_factory):
        mon = HealthMonitor(
            {"docker": lambda: _ok("docker"), "my-db": lambda: _ok("my-db")}
        )
        async with await client_factory(
            make_services(health_monitor=mon)
        ) as client:
            h = await _login(client, "researcher", "res-pw")
            resp = await client.get(
                "/api/v1/system/dependencies", headers=h
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["source"] == "monitor"
            assert {i["name"] for i in body["items"]} == {"docker", "my-db"}
            assert all(i["status"] == "up" for i in body["items"])


class TestStopEndpoints:
    @pytest_asyncio.fixture
    async def client(self, tmp_path: Path):
        budget = BudgetController(
            stop_file=tmp_path / "STOP", scope_limits={}, global_limit=10**9
        )
        app = create_app(make_services(budget_controller=budget))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c, budget

    async def test_stop_writes_stop_file_and_delete_removes(self, client):
        client, budget = client
        h = await _login(client, "admin", "admin-pw")
        resp = await client.post("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
        assert budget.stop_file.exists()  # real circuit, not just a flag
        assert budget.check_stop_file() is True

        resp = await client.delete("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
        assert not budget.stop_file.exists()  # recovery clears the file
        assert budget.check_stop_file() is False

    async def test_delete_missing_stop_file_ok(self, client):
        client, budget = client
        h = await _login(client, "admin", "admin-pw")
        resp = await client.delete("/api/v1/system:stop", headers=h)
        assert resp.status_code == 200
