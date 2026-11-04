"""Tests for rsi.log (structlog JSON logging + contextvars)."""

import json

import pytest


@pytest.fixture(autouse=True)
def _console_off(monkeypatch):
    monkeypatch.delenv("RSI_LOG_FORMAT", raising=False)


def test_logger_json_output(capsys):
    from rsi.log import get_logger

    logger = get_logger("test.module")
    logger.info("hello", run_id="run-123")
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "hello"
    assert record["run_id"] == "run-123"
    assert record["logger"] == "test.module"
    assert record["level"] == "info"


def test_contextvars_binding(capsys):
    import structlog

    from rsi.log import bind_contextvars, clear_contextvars, get_logger

    clear_contextvars()
    bind_contextvars(batch_id="batch-1", experiment_id="exp-9")
    get_logger("test.ctx").info("with ctx", run_id="run-7")
    structlog.contextvars.bind_contextvars()
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["batch_id"] == "batch-1"
    assert record["experiment_id"] == "exp-9"
    assert record["run_id"] == "run-7"
    clear_contextvars()


def test_console_format_env(monkeypatch, capsys):
    monkeypatch.setenv("RSI_LOG_FORMAT", "console")
    from rsi.log import configure_logging, get_logger

    configure_logging()
    get_logger("test.console").info("colored hello")
    out = capsys.readouterr().out
    assert "colored hello" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[-1])
    # restore JSON for other tests
    monkeypatch.delenv("RSI_LOG_FORMAT")
    configure_logging()


def test_no_contextvars_leak(capsys):
    from rsi.log import clear_contextvars, get_logger

    clear_contextvars()
    get_logger("test.clean").info("no ctx")
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert "batch_id" not in record
    assert "experiment_id" not in record
