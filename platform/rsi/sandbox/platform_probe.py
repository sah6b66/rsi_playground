"""Platform URL probe (T2, DD-04 §5.7 prebuilt mode).

eNSP Pro / PNETLab simulation platforms are web applications — the only
platform-level health signal is HTTP reachability of their Web entry URL.
Any HTTP response (2xx/3xx/401/403 — self-signed portals commonly answer
401/redirects) means the platform is online; connect/SSL/timeout errors
mean offline. Device-level SSH is a separate concern (env.yaml ssh_access).

Offline-safe by design: probe failures never raise.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel

__all__ = ["PlatformProbe", "PlatformProbeResult"]

_TIMEOUT_S = 3.0


class PlatformProbeResult(BaseModel):
    """Single probe outcome; ok=True iff any HTTP response was received."""

    ok: bool
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    detail: str = ""
    probed_at: str = ""


class PlatformProbe:
    """URL prober with a TTL cache.

    ``client`` may be injected for tests (e.g. httpx.MockTransport-backed).
    The probe owns (and closes) a client it creates; injected clients stay
    under caller ownership.
    """

    def __init__(
        self,
        url: str,
        verify_tls: bool = False,
        ttl_s: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.verify_tls = verify_tls
        self.ttl_s = ttl_s
        self._client = client
        self._cached: PlatformProbeResult | None = None
        self._cached_at: float = 0.0

    # ── internal ────────────────────────────────────────────────────

    def _request(self, client: httpx.Client) -> PlatformProbeResult:
        started = time.perf_counter()
        probed_at = datetime.now(timezone.utc).isoformat()
        try:
            resp = client.head(self.url, timeout=_TIMEOUT_S)
            if resp.status_code == 405:  # HEAD not allowed → GET fallback
                resp = client.get(self.url, timeout=_TIMEOUT_S)
            latency = (time.perf_counter() - started) * 1000.0
            return PlatformProbeResult(
                ok=True,
                status_code=resp.status_code,
                latency_ms=round(latency, 2),
                detail=f"HTTP {resp.status_code}",
                probed_at=probed_at,
            )
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - started) * 1000.0
            return PlatformProbeResult(
                ok=False,
                status_code=None,
                latency_ms=round(latency, 2),
                detail=type(exc).__name__,
                probed_at=probed_at,
            )
        except OSError as exc:  # pragma: no cover - httpx wraps most of these
            return PlatformProbeResult(
                ok=False,
                status_code=None,
                latency_ms=None,
                detail=type(exc).__name__,
                probed_at=probed_at,
            )

    # ── public API ──────────────────────────────────────────────────

    def probe(self) -> PlatformProbeResult:
        """Probe now, bypassing the cache (cache is refreshed)."""
        if self._client is not None:
            result = self._request(self._client)
        else:
            with httpx.Client(verify=self.verify_tls) as client:
                result = self._request(client)
        self._cached = result
        self._cached_at = time.monotonic()
        return result

    def probe_cached(self) -> PlatformProbeResult:
        """Return the cached result when still within TTL, else re-probe."""
        if self._cached is not None and \
                (time.monotonic() - self._cached_at) < self.ttl_s:
            return self._cached
        return self.probe()
