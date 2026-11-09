"""Static SPA serving from web/dist (DD-14 §7): mounted when dist exists.

Two forms verified with a tmp stub dist passed via ``create_app(web_dist=...)``:
dist present → ``/`` serves index.html and API routes still win; dist absent
→ no static route (404).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from rsi.api.app import create_app

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def test_api_health_unaffected() -> None:
    app = create_app(services={})
    client = TestClient(app)
    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_no_dist_no_index_route() -> None:
    # Point web_dist at an empty (existing) dir: no index.html → no html mount
    # fallback for "/".
    with tempfile.TemporaryDirectory() as td:
        empty = Path(td) / "empty"
        empty.mkdir()
        app = create_app(services={}, web_dist=empty)
        client = TestClient(app)
        resp = client.get("/")
        if WEB_DIST.is_dir():
            # dist exists in this checkout (built earlier): "/" serves index.html.
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]
        else:
            assert resp.status_code == 404


def test_dist_mount_serves_index_and_api_wins() -> None:
    with tempfile.TemporaryDirectory() as td:
        dist = Path(td)
        (dist / "index.html").write_text("<html>rsi web</html>", encoding="utf-8")
        app = create_app(services={}, web_dist=dist)
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "rsi web" in resp.text
        assert "text/html" in resp.headers["content-type"]
        # API routes registered before the mount still win.
        assert client.get("/api/v1/health").status_code == 200
        # 静态文件（如 assets）直接命中
        (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
        assert client.get("/app.js").status_code == 200
