"""ConfigApiCaller (DD-04 §5.5) — call_api vehicle for prepare/restore_task.

POSTs a JSON body to ``<base_url>/apply``; ``undo=True`` re-invokes the same
API for L1 recovery (ADR-20 — the external API contract is idempotent).
"""

from __future__ import annotations

from typing import Any

import httpx

from rsi.core.errors import EnvError
from rsi.log import get_logger
from rsi.core.models import CallApiParams, EnvSpec
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
        self._client = client  # None: per-call client (P0; S11 reuses a pool)

    async def apply(
        self,
        env: EnvSpec,
        api_key: str,
        params: CallApiParams,
        undo: bool = False,
    ) -> dict[str, Any]:
        logger.debug(
            "apply.entry", env=env.name, api=api_key, undo=undo, devices=len(params.devices)
        )
        try:
            cfg = env.config_apis[api_key]
        except KeyError:
            logger.debug("apply.error", exc_type="EnvError", api=api_key)
            raise EnvError(
                f"config api {api_key!r} not declared in env {env.name!r}",
                context={"api": api_key, "env": env.name},
            ) from None

        headers = self._auth_headers(cfg.auth_ref)
        body = {
            "env_name": params.env_name,
            "scenario": params.scenario,
            "devices": [
                {"device": e.device, "cli_lines": list(e.cli_lines)}
                for e in params.devices
            ],
            "undo": undo,
        }
        url = cfg.base_url.rstrip("/") + "/apply"

        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(
                url, json=body, headers=headers, timeout=cfg.timeout_s
            )
        except httpx.HTTPError as exc:
            logger.debug("apply.error", exc_type=type(exc).__name__, api=api_key)
            raise EnvError(
                f"config api {api_key!r} request failed: {exc}",
                context={"api": api_key, "url": url},
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()

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
