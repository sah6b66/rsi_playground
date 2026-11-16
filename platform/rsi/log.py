"""Structured logging (config doc §2 logging, design 5.2 / NFR5).

structlog with JSON output; ``RSI_LOG_FORMAT=console`` switches to colored
console rendering. Run/batch/experiment context flows via structlog
contextvars so it appears on every log line once bound.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

CONTEXT_KEYS = ("run_id", "batch_id", "experiment_id")


def _is_console() -> bool:
    return os.environ.get("RSI_LOG_FORMAT", "").lower() == "console"


class _CurrentStdout:
    """File-like proxy that resolves ``sys.stdout`` 与文件通道 at write time.

    Avoids capturing a stale/stdout wrapper at configure time (colorama on
    Windows wraps stdout and pytest's capsys replaces it per-test).

    F9-a（S11 实测）：文件通道也必须写时解析——模块级
    ``logger = get_logger(__name__)`` 在 import 期物化（先于
    configure_logging(logs_dir)），若工厂在 configure 时才绑定 _Tee，
    已存在的 logger 永远只写 stdout（app.jsonl 近乎全空，仅延迟导入
    模块的 2 行）。代理在 write 时查全局 ``_file_sink``，configure
    切换对所有 logger 立即生效。
    """

    def write(self, s: str) -> int:
        n = sys.stdout.write(s)
        sink = _file_sink
        if sink is not None:
            sink.write_file(s)
        return n

    def flush(self) -> None:
        sys.stdout.flush()
        sink = _file_sink
        if sink is not None:
            sink.flush()


_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 3
_ERROR_LEVELS = ('"level": "warning"', '"level": "error"',
                 '"level": "critical"')

# 当前文件通道（None=仅 stdout）。configure_logging 切换，_CurrentStdout
# 写时读取——见 _CurrentStdout docstring（F9-a）。
_file_sink: "_Tee | None" = None


class _Tee:
    """文件双写通道：app.jsonl 全量 + error.jsonl 仅 warning/error（F4/W9）.

    stdout 由 _CurrentStdout 负责（本类只写文件）。轮转：单文件超限时
    改名 .1/.2/.3（保留 3 代）。structlog PrintLogger 逐行调
    write(msg + LF)，按行嗅探级别即可。
    """

    def __init__(self, logs_dir) -> None:
        from pathlib import Path

        self._dir = Path(logs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # newline=LF：Windows 文本模式会把 LF 翻成 CRLF 破坏逐行 JSON
        self._app = open(
            self._dir / "app.jsonl", "a", encoding="utf-8", newline=chr(10))
        self._err = open(
            self._dir / "error.jsonl", "a", encoding="utf-8", newline=chr(10))
        self._app_bytes = self._app.tell()
        self._err_bytes = self._err.tell()
        self._err_pending = False  # error 行正文已写、待补 LF

    def _rotate(self, fh, base, written: int) -> int:
        if written < _MAX_BYTES:
            return written
        fh.close()
        for i in range(_BACKUPS - 1, 0, -1):
            src, dst = base.with_suffix(f".jsonl.{i}"), base.with_suffix(
                f".jsonl.{i + 1}")
            if src.exists():
                src.replace(dst)
        base.replace(base.with_suffix(".jsonl.1"))
        new = open(base, "a", encoding="utf-8")
        if fh is self._app:
            self._app = new
        else:
            self._err = new
        return 0

    def write_file(self, s: str) -> int:
        n = len(s.encode("utf-8", "replace"))
        try:
            self._app.write(s)
            self._app.flush()
            self._app_bytes = self._rotate(
                self._app, self._dir / "app.jsonl", self._app_bytes + n)
            # PrintLogger 分两次 write（正文、LF）——error 通道需自管换行
            if s == "\n":
                if self._err_pending:
                    self._err.write("\n")
                    self._err.flush()
                    self._err_pending = False
            elif any(m in s for m in _ERROR_LEVELS):
                self._err.write(s)
                self._err.flush()
                self._err_pending = True
                self._err_bytes = self._rotate(
                    self._err, self._dir / "error.jsonl", self._err_bytes + n)
        except OSError:
            pass  # 文件日志失败不影响 stdout 主链路
        return n

    def flush(self) -> None:
        try:
            self._app.flush()
            self._err.flush()
        except OSError:
            pass


def configure_logging(
    level: str | None = None, logs_dir=None
) -> None:
    """(Re)configure structlog + stdlib logging. JSON by default.

    ``logs_dir`` 给定时双写文件：app.jsonl 全量 + error.jsonl 仅
    warning/error（10MB×3 代轮转）——F4/W9，替代空置的 logs 目录。
    切换文件通道对 import 期已创建的 logger 同样生效（F9-a 代理 sink）。
    """
    global _file_sink
    level = level or os.environ.get("RSI_LOG_LEVEL", "info")
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # F9-a：第三方 HTTP 库的 INFO 请求日志无信号量且刷屏（S11 实测 8510
    # 条 `HTTP Request: HEAD ... 500`）——压到 WARNING；探针结果经结构化
    # HealthResult/doctor/dependencies 消费，原始行不进日志。
    for _noisy in ("httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if _is_console():
        renderer: structlog.typing.Processor = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    _file_sink = _Tee(logs_dir) if logs_dir is not None and not _is_console() else None
    structlog.configure(
        processors=[*shared, structlog.processors.UnicodeDecoder(), renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStdout()),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound with ``logger=name``."""
    return structlog.get_logger(name).bind(logger=name)


def bind_contextvars(**kwargs) -> None:
    """Bind run/batch/experiment (or any) context onto all future logs."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_contextvars() -> None:
    structlog.contextvars.clear_contextvars()


configure_logging()
