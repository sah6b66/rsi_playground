"""Tests for rsi.sandbox.platform_probe (T2)."""

from __future__ import annotations

import httpx
import pytest

from rsi.sandbox.platform_probe import PlatformProbe, PlatformProbeResult

URL = "https://192.168.56.101:8443"


class _Counter:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        return self.handler(request)


def _probe(handler, **kw) -> tuple[PlatformProbe, _Counter]:
    counter = _Counter(handler)
    client = httpx.Client(transport=httpx.MockTransport(counter), verify=False)
    return PlatformProbe(URL, client=client, **kw), counter


class TestProbeOnline:
    @pytest.mark.parametrize("status", [200, 302, 401, 403])
    def test_any_http_response_is_online(self, status):
        probe, _ = _probe(lambda req: httpx.Response(status))
        r = probe.probe()
        assert r.ok is True
        assert r.status_code == status
        assert r.latency_ms is not None
        assert r.probed_at

    def test_head_405_falls_back_to_get(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.method)
            return httpx.Response(405) if req.method == "HEAD" \
                else httpx.Response(200)

        probe, _ = _probe(handler)
        r = probe.probe()
        assert r.ok is True and r.status_code == 200
        assert seen == ["HEAD", "GET"]

    def test_head_allowed_no_get(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.method)
            return httpx.Response(204)

        probe, _ = _probe(handler)
        assert probe.probe().status_code == 204
        assert seen == ["HEAD"]


class TestProbeOffline:
    def test_connect_error_offline(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=req)

        probe, _ = _probe(handler)
        r = probe.probe()
        assert r.ok is False
        assert r.status_code is None
        assert "ConnectError" in r.detail

    def test_tls_error_offline(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("certificate verify failed", request=req)

        probe, _ = _probe(handler)
        r = probe.probe()
        assert r.ok is False
        assert r.detail

    def test_timeout_offline(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=req)

        probe, _ = _probe(handler)
        r = probe.probe()
        assert r.ok is False
        assert "Timeout" in r.detail


class TestTtlCache:
    def test_cached_within_ttl_hits_transport_once(self):
        probe, counter = _probe(lambda req: httpx.Response(200), ttl_s=60.0)
        r1 = probe.probe_cached()
        r2 = probe.probe_cached()
        assert r1.ok and r2.ok
        assert counter.calls == 1
        assert r2 is r1  # identical cached object

    def test_cache_expires_after_ttl(self):
        probe, counter = _probe(lambda req: httpx.Response(200), ttl_s=0.0)
        probe.probe_cached()
        probe.probe_cached()
        assert counter.calls == 2

    def test_probe_bypasses_cache(self):
        probe, counter = _probe(lambda req: httpx.Response(200), ttl_s=60.0)
        probe.probe()
        probe.probe()
        assert counter.calls == 2


class TestResultModel:
    def test_minimal_result(self):
        r = PlatformProbeResult(ok=False, detail="ConnectError")
        assert r.status_code is None
        assert r.latency_ms is None
        assert r.probed_at == ""
