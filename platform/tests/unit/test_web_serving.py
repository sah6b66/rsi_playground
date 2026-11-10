"""Static SPA serving from web/dist (DD-14 §7): served when dist exists.

Forms verified with a tmp stub dist passed via ``create_app(web_dist=...)``:
dist with index.html → ``/`` serves it, deep links fall back to it (T8
history fallback), static files hit, API routes still win; dist without
index.html → no static route (404).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from rsi.api.app import create_app


def _stub_dist(html: str = "<html>rsi web</html>") -> Path:
    td = tempfile.mkdtemp()
    dist = Path(td)
    (dist / "index.html").write_text(html, encoding="utf-8")
    return dist


def test_api_health_unaffected() -> None:
    app = create_app(services={})
    client = TestClient(app)
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_no_dist_no_index_route() -> None:
    # Point web_dist at an empty (existing) dir: no index.html → no fallback
    # route registered → "/" 404.
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty"
        empty.mkdir()
        app = create_app(services={}, web_dist=empty)
        client = TestClient(app)
        assert client.get("/").status_code == 404


def test_dist_serves_index_and_api_wins() -> None:
    dist = _stub_dist()
    app = create_app(services={}, web_dist=dist)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "rsi web" in resp.text
    assert "text/html" in resp.headers["content-type"]
    # API routes registered before the fallback still win.
    assert client.get("/api/v1/health").status_code == 200
    # 静态文件（如 assets）直接命中
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    assert client.get("/app.js").status_code == 200


def test_spa_history_fallback_deep_link() -> None:
    """T8：SPA 深链（/sessions/abc）刷新返回 index.html 而非 404。"""
    app = create_app(services={}, web_dist=_stub_dist("<html>spa-shell</html>"))
    client = TestClient(app)
    resp = client.get("/sessions/iter-abcdef01")
    assert resp.status_code == 200
    assert "spa-shell" in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_unmatched_api_not_swallowed_by_index() -> None:
    """T8：/api/v1/* 未匹配仍 404 JSON，不被 index.html 吞掉。"""
    app = create_app(services={}, web_dist=_stub_dist("<html>spa-shell</html>"))
    client = TestClient(app)
    resp = client.get("/api/v1/no-such-endpoint")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    assert "spa-shell" not in resp.text
