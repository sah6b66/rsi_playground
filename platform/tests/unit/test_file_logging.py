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


def test_import_time_logger_double_writes_after_configure(tmp_path: Path):
    """F9-a（S11 实测）：模块级 ``logger = get_logger(__name__)`` 在 import
    期创建（先于 configure_logging(logs_dir)），此前绑定的 stdout 工厂
    导致文件日志近乎全空——app.jsonl 只有 2 行延迟导入模块的事件。
    修复后 sink 为写时解析的代理：configure 切换文件通道对所有已存在
    logger 立即生效。"""
    import rsi.log as logmod

    # 模拟 import 期：无 logs_dir 配置下先创建 logger（已存在配置亦同）
    logmod.configure_logging(level="info")
    early_logger = logmod.get_logger("test.early")

    logmod.configure_logging(level="info", logs_dir=tmp_path)
    early_logger.info("early-logger-late-event")
    import sys
    sys.stdout.flush()

    lines = [
        json.loads(l)
        for l in (tmp_path / "app.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    assert any(e.get("event") == "early-logger-late-event" for e in lines)

    logmod.configure_logging(level="info")


def test_third_party_http_loggers_quieted(tmp_path: Path):
    """F9-a：httpx/httpcore 的 INFO 请求日志无信号量且刷屏（S11 实测
    8510 条 `HTTP Request: HEAD ... 500`），压到 WARNING——探针结果经
    结构化 HealthResult 消费，原始行不再进日志。"""
    import logging

    import rsi.log as logmod

    logmod.configure_logging(level="info", logs_dir=tmp_path)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)

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
