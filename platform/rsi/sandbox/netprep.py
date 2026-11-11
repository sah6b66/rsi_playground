"""NetworkPrepClient — system-level network environment preparation API client (ADR-27, DD-04 §5.8).

Independent from the deprecated per-evalset ConfigApiCaller; talks to the
externally-managed Network Prep API (three endpoints) configured at
``settings.network_prep_api.url``.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from rsi.core.errors import ConfigError, EnvError
from rsi.log import get_logger

__all__ = ["NetworkPrepClient", "PrepResult", "RestoreResult"]

BODY_SUMMARY_LEN = 300

logger = get_logger(__name__)


class PrepResult(BaseModel):
    """Successful prepare response payload."""

    env_id: str
    status: str = "ready"


class RestoreResult(BaseModel):
    """Successful restore response payload."""

    status: str = "restored"


class NetworkPrepClient:
    """Network Prep API v1 contract client (external system).

    Holds ONE lazily-created ``httpx.AsyncClient`` shared by all three
    endpoints (connection pooling, same lifecycle pattern as ConfigApiCaller).
    """

    def __init__(self, base_url: str, timeout_s: int = 120) -> None:
        if not base_url:
            raise ConfigError(
                "network prep API url is empty (API prep mode unavailable)",
                context={"base_url": base_url},
            )
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def prepare(self, env_name: str, params: dict, session_id: str = "") -> PrepResult:
        """POST /environments/{env_name}/prepare — non-2xx/timeout -> EnvError."""
        url = f"{self.base_url}/environments/{env_name}/prepare"
        data = await self._post(url, {"params": params, "session_id": session_id}, "prepare", env_name)
        try:
            payload = PrepResult.model_validate(data)
        except ValueError as exc:
            raise EnvError(
                f"network prep prepare failed for {env_name}: invalid response: {exc}",
                context={"env_name": env_name, "url": url},
            ) from exc
        return payload

    async def restore(self, env_name: str, env_id: str, params: dict) -> RestoreResult:
        """POST /environments/{env_name}/restore — non-2xx/timeout -> EnvError."""
        url = f"{self.base_url}/environments/{env_name}/restore"
        data = await self._post(url, {"env_id": env_id, "params": params}, "restore", env_name)
        try:
            payload = RestoreResult.model_validate(data)
        except ValueError as exc:
            raise EnvError(
                f"network prep restore failed for {env_name}: invalid response: {exc}",
                context={"env_name": env_name, "url": url},
            ) from exc
        return payload

    async def status(self, env_name: str) -> bool:
        """GET /environments/{env_name}/status -> ready flag.

        Probe semantics (consumed by HealthMonitor/polling loops): ANY
        failure (connection error, timeout, non-2xx, bad body) returns False
        instead of raising.
        """
        url = f"{self.base_url}/environments/{env_name}/status"
        try:
            client = self._get_client()
            resp = await client.get(url, timeout=self.timeout_s)
            if resp.status_code != 200:
                logger.warning(
                    "netprep.status.non_2xx",
                    env_name=env_name,
                    status_code=resp.status_code,
                )
                return False
            return bool(resp.json().get("ready", False))
        except Exception:
            logger.debug("netprep.status.error", env_name=env_name)
            return False

    async def aclose(self) -> None:
        """Close the owned client; reset so a later call recreates one."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create one client and reuse it (connection pooling)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def _post(
        self, url: str, body: dict[str, Any], op: str, env_name: str
    ) -> dict[str, Any]:
        try:
            client = self._get_client()
            resp = await client.post(url, json=body, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise EnvError(
                f"network prep {op} failed for {env_name}: {exc}",
                context={"env_name": env_name, "url": url, "exc": type(exc).__name__},
            ) from exc
        if resp.status_code != 200:
            summary = resp.text[:BODY_SUMMARY_LEN]
            raise EnvError(
                f"network prep {op} failed for {env_name}: "
                f"HTTP {resp.status_code}: {summary}",
                context={
                    "env_name": env_name,
                    "url": url,
                    "status_code": resp.status_code,
                },
            )
        try:
            return dict(resp.json())
        except ValueError:
            return {"raw": resp.text}
