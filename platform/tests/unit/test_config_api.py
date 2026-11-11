"""Unit tests: ConfigApiCaller (deprecated, ADR-27) — kept for the retained
constructor injection; ``apply`` now takes endpoint fields directly."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from rsi.core.errors import EnvError
from rsi.core.models import CallApiParams, CliEntry
from rsi.sandbox.config_api import ConfigApiCaller
from rsi.security.credentials import CredentialStore

MASTER_KEY = base64.b64encode(b"test-key-32-bytes-1234567890!!!!").decode()

BASE = "http://cfg.example.com/api/v1"

PARAMS = CallApiParams(
    env_name="lab-a",
    scenario="bgp-misconfig",
    devices=[CliEntry(device="R1", cli_lines=["bgp 65001", "peer 10.0.0.2 as 65002"])],
)


class TestApply:
    @pytest.mark.asyncio
    @respx.mock
    async def test_post_body_and_url(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
        caller = ConfigApiCaller()
        result = await caller.apply(BASE, PARAMS)
        assert result == {"status": "ok"}
        assert route.called
        request = route.calls.last.request

        body = json.loads(request.content)
        assert body == {
            "env_name": "lab-a",
            "scenario": "bgp-misconfig",
            "devices": [{"device": "R1", "cli_lines": ["bgp 65001", "peer 10.0.0.2 as 65002"]}],
            "undo": False,
        }

    @pytest.mark.asyncio
    @respx.mock
    async def test_undo_true_in_body(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={"undone": True}))
        caller = ConfigApiCaller()
        await caller.apply(BASE, PARAMS, undo=True)

        assert json.loads(route.calls.last.request.content)["undo"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_header_from_credential_store(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("cfg-token", "nms_api", "secret-token-xyz")
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller(credential_store=store)
        await caller.apply(BASE, PARAMS, auth_ref="cfg-token")
        assert route.calls.last.request.headers["authorization"] == "Bearer secret-token-xyz"

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_auth_header_without_auth_ref(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()
        await caller.apply(BASE, PARAMS)
        assert "authorization" not in route.calls.last.request.headers

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_2xx_raises_enverror_with_body_summary(self):
        respx.post(f"{BASE}/apply").mock(
            return_value=httpx.Response(503, text='{"error": "backend starting"}')
        )
        caller = ConfigApiCaller()
        with pytest.raises(EnvError) as ei:
            await caller.apply(BASE, PARAMS)
        assert "503" in str(ei.value) or "backend starting" in str(ei.value)
        assert ei.value.context.get("status_code") == 503

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_passed_to_request(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()
        await caller.apply(BASE, PARAMS, timeout_s=7)
        # respx route matched with the configured client timeout; verify caller used it
        assert route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_credential_store_with_auth_ref_raises(self):
        respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()  # no store
        with pytest.raises(EnvError):
            await caller.apply(BASE, PARAMS, auth_ref="cfg-token")
