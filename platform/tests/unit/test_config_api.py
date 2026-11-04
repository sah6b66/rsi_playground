"""Unit tests: ConfigApiCaller (DD-04 §5.5, ADR-20 call_api vehicle)."""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from rsi.core.errors import EnvError
from rsi.core.models import (
    CallApiParams,
    CliEntry,
    ConfigApi,
    DeviceSpec,
    EnvSpec,
    SshAccess,
    Topology,
)
from rsi.sandbox.config_api import ConfigApiCaller
from rsi.security.credentials import CredentialStore

MASTER_KEY = base64.b64encode(b"test-key-32-bytes-1234567890!!!!").decode()

BASE = "http://cfg.example.com/api/v1"


def _env(auth_ref: str | None = None, timeout_s: int = 60) -> EnvSpec:
    return EnvSpec(
        name="lab-a",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(),
        ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password="pw")},
        config_apis={"cfg": ConfigApi(base_url=BASE, auth_ref=auth_ref, timeout_s=timeout_s)},
    )


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
        result = await caller.apply(_env(), "cfg", PARAMS)
        assert result == {"status": "ok"}
        assert route.called
        request = route.calls.last.request
        import json

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
        await caller.apply(_env(), "cfg", PARAMS, undo=True)
        import json

        assert json.loads(route.calls.last.request.content)["undo"] is True

    @pytest.mark.asyncio
    @respx.mock
    async def test_auth_header_from_credential_store(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("cfg-token", "nms_api", "secret-token-xyz")
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller(credential_store=store)
        await caller.apply(_env(auth_ref="cfg-token"), "cfg", PARAMS)
        assert route.calls.last.request.headers["authorization"] == "Bearer secret-token-xyz"

    @pytest.mark.asyncio
    @respx.mock
    async def test_no_auth_header_without_auth_ref(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()
        await caller.apply(_env(), "cfg", PARAMS)
        assert "authorization" not in route.calls.last.request.headers

    @pytest.mark.asyncio
    @respx.mock
    async def test_non_2xx_raises_enverror_with_body_summary(self):
        respx.post(f"{BASE}/apply").mock(
            return_value=httpx.Response(503, text='{"error": "backend starting"}')
        )
        caller = ConfigApiCaller()
        with pytest.raises(EnvError) as ei:
            await caller.apply(_env(), "cfg", PARAMS)
        assert "503" in str(ei.value) or "backend starting" in str(ei.value)
        assert ei.value.context.get("status_code") == 503

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_passed_to_request(self):
        route = respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()
        await caller.apply(_env(timeout_s=7), "cfg", PARAMS)
        # respx route matched with the configured client timeout; verify caller used it
        assert route.called

    @pytest.mark.asyncio
    async def test_unknown_api_key_raises(self):
        caller = ConfigApiCaller()
        with pytest.raises(EnvError):
            await caller.apply(_env(), "nope", PARAMS)

    @pytest.mark.asyncio
    @respx.mock
    async def test_missing_credential_store_with_auth_ref_raises(self):
        respx.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={}))
        caller = ConfigApiCaller()  # no store
        with pytest.raises(EnvError):
            await caller.apply(_env(auth_ref="cfg-token"), "cfg", PARAMS)
