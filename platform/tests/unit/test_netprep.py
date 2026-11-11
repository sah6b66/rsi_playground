"""Unit tests: NetworkPrepClient (DD-04 §5.8, ADR-27) — TU-038."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from rsi.core.errors import ConfigError, EnvError
from rsi.sandbox.netprep import NetworkPrepClient

BASE = "http://netprep.example.com/api"


def _client() -> NetworkPrepClient:
    return NetworkPrepClient(BASE, timeout_s=30)


class TestPrepare:
    @pytest.mark.asyncio
    @respx.mock
    async def test_prepare_200(self):
        route = respx.post(f"{BASE}/environments/lab-a/prepare").mock(
            return_value=httpx.Response(200, json={"env_id": "env-abc", "status": "ready"})
        )
        client = _client()
        result = await client.prepare("lab-a", {"vlan": 10}, session_id="s-1")
        assert result.env_id == "env-abc"
        assert result.status == "ready"
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body == {"params": {"vlan": 10}, "session_id": "s-1"}

    @pytest.mark.asyncio
    @respx.mock
    async def test_prepare_500_raises_enverror(self):
        respx.post(f"{BASE}/environments/lab-a/prepare").mock(
            return_value=httpx.Response(500, text="boom")
        )
        client = _client()
        with pytest.raises(EnvError) as exc_info:
            await client.prepare("lab-a", {})
        assert exc_info.value.context["status_code"] == 500
        assert "network prep prepare failed for lab-a" in str(exc_info.value)

    @pytest.mark.asyncio
    @respx.mock
    async def test_prepare_timeout_raises_enverror(self):
        respx.post(f"{BASE}/environments/lab-a/prepare").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        client = _client()
        with pytest.raises(EnvError):
            await client.prepare("lab-a", {})


class TestRestore:
    @pytest.mark.asyncio
    @respx.mock
    async def test_restore_200(self):
        route = respx.post(f"{BASE}/environments/lab-a/restore").mock(
            return_value=httpx.Response(200, json={"status": "restored"})
        )
        client = _client()
        result = await client.restore("lab-a", "env-abc", {"vlan": 10})
        assert result.status == "restored"
        body = json.loads(route.calls.last.request.content)
        assert body == {"env_id": "env-abc", "params": {"vlan": 10}}

    @pytest.mark.asyncio
    @respx.mock
    async def test_restore_500_raises_enverror(self):
        respx.post(f"{BASE}/environments/lab-a/restore").mock(
            return_value=httpx.Response(500, text="boom")
        )
        client = _client()
        with pytest.raises(EnvError) as exc_info:
            await client.restore("lab-a", "env-abc", {})
        assert exc_info.value.context["status_code"] == 500


class TestStatus:
    @pytest.mark.asyncio
    @respx.mock
    async def test_status_ready_true(self):
        respx.get(f"{BASE}/environments/lab-a/status").mock(
            return_value=httpx.Response(200, json={"ready": True})
        )
        assert await _client().status("lab-a") is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_status_ready_false(self):
        respx.get(f"{BASE}/environments/lab-a/status").mock(
            return_value=httpx.Response(200, json={"ready": False})
        )
        assert await _client().status("lab-a") is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_status_connection_error_returns_false(self):
        respx.get(f"{BASE}/environments/lab-a/status").mock(
            side_effect=httpx.ConnectError("refused")
        )
        assert await _client().status("lab-a") is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_status_non_200_returns_false(self):
        respx.get(f"{BASE}/environments/lab-a/status").mock(
            return_value=httpx.Response(503, text="unavailable")
        )
        assert await _client().status("lab-a") is False


class TestClientLifecycle:
    @pytest.mark.asyncio
    @respx.mock
    async def test_client_reused_across_calls(self):
        respx.post(f"{BASE}/environments/lab-a/prepare").mock(
            return_value=httpx.Response(200, json={"env_id": "e1", "status": "ready"})
        )
        client = _client()
        await client.prepare("lab-a", {})
        first = client._client
        await client.prepare("lab-a", {})
        assert client._client is first
        assert first is not None

    @pytest.mark.asyncio
    @respx.mock
    async def test_aclose_resets_client(self):
        respx.post(f"{BASE}/environments/lab-a/prepare").mock(
            return_value=httpx.Response(200, json={"env_id": "e1", "status": "ready"})
        )
        client = _client()
        await client.prepare("lab-a", {})
        await client.aclose()
        assert client._client is None

    def test_empty_base_url_raises_configerror(self):
        with pytest.raises(ConfigError):
            NetworkPrepClient("")
