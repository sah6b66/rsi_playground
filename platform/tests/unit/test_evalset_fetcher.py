"""Unit tests: EvalsetFetcher + GET /evalsets/online/{name} (ADR-27) — TU-039."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
import httpx
import respx

from rsi.api.app import create_app
from rsi.core.errors import EnvError, NotFoundError, ValidationError
from rsi.core.models import ApiEvalsetTask
from rsi.sandbox.evalset_fetcher import EvalsetFetcher
from rsi.security.audit import AuditLog
from rsi.security.auth import AuthService

BASE = "http://evalsets.example.com/api"

pytestmark = pytest.mark.asyncio


def _task(task_id: str = "t-001", env: str | None = "lab-bgp-3node") -> dict:
    t: dict = {
        "task_id": task_id,
        "title": f"title-{task_id}",
        "task": {"prompt": "fix it, FINAL_JSON"},
        "expected": {"root_cause": {"expected": "bad peer config"}},
    }
    if env is not None:
        t["network_env"] = {"name": env, "params": {}}
    return t


def _fetcher() -> EvalsetFetcher:
    return EvalsetFetcher(BASE, timeout_s=30)


# ---------------------------------------------------------------------------
# EvalsetFetcher.fetch
# ---------------------------------------------------------------------------


class TestFetch:
    @respx.mock
    async def test_fetch_200_valid(self):
        respx.get(f"{BASE}/evalsets/test-es").mock(
            return_value=httpx.Response(
                200,
                json={"name": "test-es", "tasks": [_task("t-001"), _task("t-002", "lab-ospf")]},
            )
        )
        tasks = await _fetcher().fetch("test-es")
        assert isinstance(tasks, list)
        assert all(isinstance(t, ApiEvalsetTask) for t in tasks)
        assert len(tasks) == 2
        assert tasks[0].task_id == "t-001"
        assert tasks[0].network_env.name == "lab-bgp-3node"

    @respx.mock
    async def test_fetch_invalid_task_raises_validationerror_with_task_id(self):
        respx.get(f"{BASE}/evalsets/test-es").mock(
            return_value=httpx.Response(
                200,
                json={"name": "test-es", "tasks": [_task("t-001"), _task("t-002", env=None)]},
            )
        )
        with pytest.raises(ValidationError) as exc_info:
            await _fetcher().fetch("test-es")
        assert "t-002" in str(exc_info.value)
        assert exc_info.value.context["task_id"] == "t-002"

    @respx.mock
    async def test_fetch_404_raises_notfounderror(self):
        respx.get(f"{BASE}/evalsets/missing-es").mock(
            return_value=httpx.Response(404, text="not found")
        )
        with pytest.raises(NotFoundError) as exc_info:
            await _fetcher().fetch("missing-es")
        assert "missing-es" in str(exc_info.value)
        assert "not found on online API" in str(exc_info.value)

    @respx.mock
    async def test_fetch_500_raises_enverror(self):
        respx.get(f"{BASE}/evalsets/test-es").mock(
            return_value=httpx.Response(500, text="boom")
        )
        with pytest.raises(EnvError) as exc_info:
            await _fetcher().fetch("test-es")
        assert exc_info.value.context["status_code"] == 500

    @respx.mock
    async def test_fetch_timeout_raises_enverror(self):
        respx.get(f"{BASE}/evalsets/test-es").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        with pytest.raises(EnvError):
            await _fetcher().fetch("test-es")


class TestLifecycle:
    @respx.mock
    async def test_client_reused_and_aclose_resets(self):
        respx.get(f"{BASE}/evalsets/test-es").mock(
            return_value=httpx.Response(200, json={"name": "test-es", "tasks": []})
        )
        fetcher = _fetcher()
        await fetcher.fetch("test-es")
        first = fetcher._client
        await fetcher.fetch("test-es")
        assert fetcher._client is first
        await fetcher.aclose()
        assert fetcher._client is None

    def test_empty_url_constructed_not_raising(self):
        fetcher = EvalsetFetcher("")
        assert fetcher.base_url == ""


# ---------------------------------------------------------------------------
# GET /evalsets/online/{name}
# ---------------------------------------------------------------------------


class StubFetcher:
    def __init__(self, tasks: list[ApiEvalsetTask] | None = None, url: str = BASE) -> None:
        self.base_url = url.rstrip("/")
        self._tasks = tasks or []

    async def fetch(self, name: str) -> list[ApiEvalsetTask]:
        return self._tasks


def make_services(**extra) -> dict:
    auth = AuthService(secret="test-secret")
    services = {
        "auth_service": auth,
        "audit_log": AuditLog(),
        "users": {
            "researcher": {
                "user_id": "u-res",
                "role": "researcher",
                "password_hash": auth.hash_password("res-pw"),
            },
        },
    }
    services.update(extra)
    return services


async def _login(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "researcher", "password": "res-pw"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest_asyncio.fixture
async def client_factory():
    async def _make(services):
        app = create_app(services)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return _make


def _api_task(task_id: str, env: str) -> ApiEvalsetTask:
    return ApiEvalsetTask.model_validate(_task(task_id, env))


class TestOnlineEndpoint:
    async def test_preview_200(self, client_factory):
        fetcher = StubFetcher(
            [_api_task("t-001", "lab-bgp"), _api_task("t-002", "lab-ospf")]
        )
        async with await client_factory(
            make_services(evalset_fetcher=fetcher)
        ) as client:
            h = await _login(client)
            resp = await client.get("/api/v1/evalsets/online/test-es", headers=h)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["name"] == "test-es"
            assert body["task_count"] == 2
            assert body["tasks"][0]["task_id"] == "t-001"
            assert body["tasks"][0]["network_env_name"]
            assert body["api_url"].endswith("/evalsets/test-es")

    async def test_unconfigured_url_409(self, client_factory):
        async with await client_factory(
            make_services(evalset_fetcher=StubFetcher(url=""))
        ) as client:
            h = await _login(client)
            resp = await client.get("/api/v1/evalsets/online/test-es", headers=h)
            assert resp.status_code == 409, resp.text
            assert "未配置" in resp.json()["message"]

    async def test_missing_service_409(self, client_factory):
        async with await client_factory(make_services()) as client:
            h = await _login(client)
            resp = await client.get("/api/v1/evalsets/online/test-es", headers=h)
            assert resp.status_code == 409

    async def test_not_found_passthrough(self, client_factory):
        class ErrFetcher(StubFetcher):
            async def fetch(self, name: str) -> list[ApiEvalsetTask]:
                raise NotFoundError(f"evalset {name!r} not found on online API")

        async with await client_factory(
            make_services(evalset_fetcher=ErrFetcher())
        ) as client:
            h = await _login(client)
            resp = await client.get("/api/v1/evalsets/online/missing-es", headers=h)
            assert resp.status_code == 404, resp.text
