"""F4/W9：轮转文件日志（app.jsonl + error.jsonl）与 /system/logs 真实读取."""
import json
from pathlib import Path


def test_configure_logging_writes_files(tmp_path: Path):
    import rsi.log as logmod

    logmod.configure_logging(level="info", logs_dir=tmp_path)
    logger = logmod.get_logger("test.sink")
    logger.info("hello-info", run_id="r1")
    logger.warning("hello-warn")
    logger.error("hello-error")
    # flush（PrintLogger 每条即写；保险 flush tee）
    import sys
    sys.stdout.flush()

    def _events(path):
        out = []
        for l in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(l))
            except json.JSONDecodeError:
                continue  # 环境噪音行
        return out

    lines = _events(tmp_path / "app.jsonl")
    assert any(e.get("event") == "hello-info" for e in lines)
    assert any(e.get("event") == "hello-warn" for e in lines)

    elines = _events(tmp_path / "error.jsonl")
    events = {e.get("event") for e in elines}
    assert "hello-error" in events and "hello-warn" in events
    assert "hello-info" not in events

    # 还原全局 stdout-only 配置，避免污染其他测试
    logmod.configure_logging(level="info")


def test_system_logs_reads_file_tail(tmp_path: Path, monkeypatch):
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from rsi.api.app import create_app
    from tests.unit.test_api_core import admin_headers, make_services

    (tmp_path / "app.jsonl").write_text(
        "\n".join(json.dumps({
            "timestamp": f"2026-11-13T00:00:{i:02d}Z", "level": "info",
            "event": f"evt-{i}", "logger": "rsi.test",
        }) for i in range(5)) + "\n",
        encoding="utf-8",
    )
    services = make_services()
    services["settings"] = type("S", (), {
        "home": tmp_path.parent, "logs_dir": tmp_path, "model_dump": dict,
    })()
    app = create_app(services)

    async def run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = await admin_headers(client)
            r = await client.get("/api/v1/system/logs?limit=3", headers=headers)
            assert r.status_code == 200
            items = r.json()["items"]
            assert [i["message"] for i in items] == ["evt-4", "evt-3", "evt-2"]

    asyncio.run(run())
