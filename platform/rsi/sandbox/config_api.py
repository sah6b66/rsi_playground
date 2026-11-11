"""DEPRECATED (ADR-27): per-evalset ConfigApiCaller superseded by
system-level NetworkPrepClient (netprep.py). Do not add new consumers.

ConfigApiCaller (DD-04 §5.5) — call_api vehicle for prepare/restore_task.

POSTs a JSON body to ``<base_url>/apply``; ``undo=True`` re-invokes the same
API for L1 recovery (ADR-20 — the external API contract is idempotent).
The per-evalset ``env.config_apis`` table was removed by ADR-27, so ``apply``
now takes the endpoint fields (base_url/auth_ref/timeout_s) directly.
"""

from __future__ import annotations

from typing import Any

import httpx

from rsi.core.errors import EnvError
from rsi.log import get_logger
from rsi.core.models import CallApiParams
from rsi.security.credentials import CredentialStore

__all__ = ["ConfigApiCaller"]

BODY_SUMMARY_LEN = 300

logger = get_logger(__name__)


class ConfigApiCaller:
    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = credential_store
        self._client = client
        # M6: when no client is injected, lazily create ONE client on first
        # call and reuse it (connection pooling) instead of one per call.
        self._owns_client = client is None

    async def apply(
        self,
        base_url: str,
        params: CallApiParams,
        auth_ref: str | None = None,
        timeout_s: int = 60,
        undo: bool = False,
    ) -> dict[str, Any]:
        logger.debug(
            "apply.entry", base_url=base_url, undo=undo, devices=len(params.devices)
        )
        api_key = base_url

        headers = self._auth_headers(auth_ref)
        body = {
            "env_name": params.env_name,
            "scenario": params.scenario,
            "devices": [
                {"device": e.device, "cli_lines": list(e.cli_lines)}
                for e in params.devices
            ],
            "undo": undo,
        }
        url = base_url.rstrip("/") + "/apply"

        client = self._client
        if client is None:
            client = httpx.AsyncClient()
            self._client = client
        try:
            resp = await client.post(
                url, json=body, headers=headers, timeout=timeout_s
            )
        except httpx.HTTPError as exc:
            logger.debug("apply.error", exc_type=type(exc).__name__, api=api_key)
            raise EnvError(
                f"config api {api_key!r} request failed: {exc}",
                context={"api": api_key, "url": url},
            ) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            summary = resp.text[:BODY_SUMMARY_LEN]
            logger.debug("apply.error", api=api_key, status_code=resp.status_code)
            raise EnvError(
                f"config api {api_key!r} returned {resp.status_code}: {summary}",
                context={"api": api_key, "url": url, "status_code": resp.status_code},
            )
        try:
            result = dict(resp.json())
        except ValueError:
            result = {"raw": resp.text}
        logger.debug("apply.exit", status_code=resp.status_code, result_type=type(result).__name__)
        return result

    async def aclose(self) -> None:
        """Close the owned client (M6); injected clients stay with the owner."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self, auth_ref: str | None) -> dict[str, str]:
        """Resolve auth_ref via CredentialStore; None -> no auth header.

        P0 pass-through: Bearer token header. The store is optional at
        construction but required when the ConfigApi declares an auth_ref.
        """
        if auth_ref is None:
            return {}
        if self._store is None:
            raise EnvError(
                f"auth_ref {auth_ref!r} declared but no CredentialStore provided",
                context={"auth_ref": auth_ref},
            )
        token = self._store.resolve(auth_ref)
        return {"Authorization": f"Bearer {token}"}
