"""opencode 会话存档（SQLite）→ native TraceEvent ndjson 行转换（S11 A1）.

opencode 1.19.5 无 prompt-logger 插件（设计附 C 勘误），会话数据落容器内
``~/.local/share/opencode/opencode.db``。exec 后由适配器回收该库并在此
转换为既有 ``tail_ndjson`` 可直接摄取的 native TraceEvent 行：

- ``part(type=tool)`` → ``tool_call`` + ``tool_result``（命令全文留在
  ``data`` 里——det 裁判按 data 全文匹配 evidence）；
- ``session`` 行的真实用量 → 一条 ``llm_request``（``usage.estimated=False``，
  覆盖估算——DD-07 §5.2 实测口径优先）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rsi.log import get_logger

__all__ = ["OPENCODE_DB_PATH", "session_archive_to_event_lines"]

logger = get_logger(__name__)

OPENCODE_DB_PATH = "/root/.local/share/opencode/opencode.db"

_OUTPUT_CAP = 4000


def _ms_to_iso(ms: Any) -> str:
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _connect_bytes(db_bytes: bytes) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.deserialize(db_bytes)
    return conn


def session_archive_to_event_lines(db_bytes: bytes, run_id: str) -> list[str]:
    """opencode.db 字节流 → native TraceEvent ndjson 行（空/坏库返回 []）."""
    if not db_bytes:
        return []
    try:
        conn = _connect_bytes(db_bytes)
    except sqlite3.Error:
        logger.warning("opencode_archive.unreadable_db", size=len(db_bytes))
        return []
    try:
        lines: list[str] = []
        for call, result in _iter_tool_parts(conn):
            if call:
                call["run_id"] = run_id
                lines.append(json.dumps(call, ensure_ascii=False))
            if result:
                result["run_id"] = run_id
                lines.append(json.dumps(result, ensure_ascii=False))
        usage_event = _session_usage_event(conn)
        if usage_event is not None:
            usage_event["run_id"] = run_id
            lines.append(json.dumps(usage_event, ensure_ascii=False))
        return lines
    finally:
        conn.close()


def _iter_tool_parts(conn: sqlite3.Connection):
    try:
        rows = conn.execute(
            "SELECT time_created, time_updated, data FROM part"
        ).fetchall()
    except sqlite3.Error:
        return
    parts: list[tuple[int, dict]] = []
    for created, updated, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "tool":
            continue
        try:
            parts.append((int(created), data))
        except (TypeError, ValueError):
            continue
    parts.sort(key=lambda p: p[0])
    for created, data in parts:
        state = data.get("state") or {}
        tool_input = state.get("input") or {}
        command = (
            tool_input.get("command")
            if isinstance(tool_input, dict)
            else None
        )
        call_id = str(data.get("callID") or "")
        tool_name = str(data.get("tool") or "")
        metadata = state.get("metadata") or {}
        time_obj = state.get("time") or {}
        start_ms = time_obj.get("start", created)
        end_ms = time_obj.get("end", updated)

        call = {
            "event_type": "tool_call",
            "seq": 0,  # collector.ingest 重排
            "ts": _ms_to_iso(start_ms),
            "data": {
                "tool": tool_name,
                "call_id": call_id,
                "command": command or json.dumps(tool_input, ensure_ascii=False),
                "input": tool_input,
            },
        }
        output = state.get("output")
        output_text = (
            output if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False) if output is not None else ""
        )
        result = {
            "event_type": "tool_result",
            "seq": 0,
            "ts": _ms_to_iso(end_ms),
            "data": {
                "call_id": call_id,
                "tool": tool_name,
                "output": output_text[:_OUTPUT_CAP],
                "exit_code": metadata.get("exit") if isinstance(metadata, dict) else None,
            },
        }
        yield call, result


def _session_usage_event(conn: sqlite3.Connection) -> dict | None:
    try:
        rows = conn.execute(
            "SELECT tokens_input, tokens_output, model, cost FROM session"
        ).fetchall()
    except sqlite3.Error:
        return None
    input_tokens = sum(r[0] or 0 for r in rows)
    output_tokens = sum(r[1] or 0 for r in rows)
    if not input_tokens and not output_tokens:
        return None
    model_id = ""
    for r in rows:
        if r[2]:
            try:
                model_id = str(json.loads(r[2]).get("id") or "")
            except (TypeError, json.JSONDecodeError):
                model_id = ""
            break
    return {
        "event_type": "llm_request",
        "seq": 0,
        "ts": "",
        "model": model_id or None,
        "data": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated": False,
            },
            "sessions": len(rows),
        },
    }
