"""opencode 会话存档（SQLite）→ native TraceEvent ndjson 转换器（F1/A1）.

opencode 1.19.5 把会话数据存容器内 ``~/.local/share/opencode/opencode.db``
（无 prompt-logger 插件可依赖——S11 实测）。转换器把 tool part 变
tool_call/tool_result 事件行（det 裁判对 data 全文匹配 evidence）、
session 真实用量变 llm_request.usage（estimated=False）。
"""
import json
import sqlite3

from rsi.carrier.opencode_archive import session_archive_to_event_lines


def _build_db(tool_parts: list[dict], session_usage: dict | None = None) -> bytes:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                           time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, cost REAL,
                              tokens_input INTEGER, tokens_output INTEGER,
                              model TEXT);
        """
    )
    for i, p in enumerate(tool_parts):
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?)",
            (f"prt-{i}", f"msg-{i}", "ses-1", p["start"], p["end"],
             json.dumps(p["raw"], ensure_ascii=False)),
        )
    usage = session_usage or {"tokens_input": 14868, "tokens_output": 16}
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?)",
        ("ses-1", "probe", 0.0, usage["tokens_input"], usage["tokens_output"],
         '{"id":"glm-5.3-flash","providerID":"zhipuai-coding-plan"}'),
    )
    conn.commit()
    return conn.serialize()


TOOL_BASH = {
    "start": 1794539366620,
    "end": 1794539366690,
    "raw": {
        "type": "tool",
        "tool": "bash",
        "callID": "call_abc",
        "state": {
            "status": "completed",
            "input": {"command": "bash /agent-config/skills/network-ops/huawei-cli.sh Spine-01 'display version'"},
            "output": "Huawei Versatile Routing Platform...",
            "metadata": {"exit": 0, "truncated": False},
            "title": "display version",
        },
    },
}

TOOL_TEXT_PART = {
    "start": 1794539366100,
    "end": 1794539366200,
    "raw": {"type": "text", "text": "assistant prose"},
}


def test_tool_parts_become_call_and_result_lines():
    db = _build_db([TOOL_BASH, TOOL_TEXT_PART])
    lines = session_archive_to_event_lines(db, run_id="run-1")
    events = [json.loads(l) for l in lines]
    types = [e["event_type"] for e in events]
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1

    call = next(e for e in events if e["event_type"] == "tool_call")
    assert call["run_id"] == "run-1"
    assert call["data"]["tool"] == "bash"
    assert call["data"]["call_id"] == "call_abc"
    # det 裁判按 data 全文匹配 evidence——命令全文必须在事件里
    blob = json.dumps(call["data"], ensure_ascii=False)
    assert "display version" in blob and "huawei-cli.sh" in blob

    result = next(e for e in events if e["event_type"] == "tool_result")
    assert result["data"]["call_id"] == "call_abc"
    assert result["data"]["exit_code"] == 0
    assert "Versatile Routing" in result["data"]["output"]


def test_session_usage_becomes_llm_request_with_measured_tokens():
    db = _build_db([TOOL_BASH], session_usage={"tokens_input": 1234, "tokens_output": 56})
    events = [json.loads(l) for l in session_archive_to_event_lines(db, "run-2")]
    usage_events = [e for e in events if e["event_type"] == "llm_request"]
    assert len(usage_events) == 1
    usage = usage_events[0]["data"]["usage"]
    assert usage["input_tokens"] == 1234
    assert usage["output_tokens"] == 56
    assert usage["estimated"] is False
    assert usage_events[0]["model"] == "glm-5.3-flash"


def test_empty_or_corrupt_db_yields_no_lines():
    assert session_archive_to_event_lines(b"", "run-3") == []
    assert session_archive_to_event_lines(b"not a db", "run-3") == []
    # 无 tool part 且无用量
    empty = _build_db([], session_usage={"tokens_input": 0, "tokens_output": 0})
    assert session_archive_to_event_lines(empty, "run-3") == []


def test_lines_are_valid_native_trace_events():
    from rsi.core.models import TraceEvent

    db = _build_db([TOOL_BASH])
    for line in session_archive_to_event_lines(db, "run-4"):
        obj = json.loads(line)
        obj.setdefault("task_id", "t")
        obj.setdefault("lab_id", "l")
        obj.setdefault("sut_id", "s")
        ev = TraceEvent.model_validate(obj)
        assert ev.run_id == "run-4"


def test_wal_mode_db_still_converted(tmp_path):
    """E2 实测回归：真库 WAL 模式下 deserialize 报 unable to open——
    临时文件连接必须兼容。"""
    import sqlite3 as sq

    path = tmp_path / "wal.db"
    conn = sq.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, time_updated INTEGER, data TEXT);"
        "CREATE TABLE session (id TEXT, title TEXT, cost REAL,"
        " tokens_input INTEGER, tokens_output INTEGER, model TEXT);"
    )
    conn.execute(
        "INSERT INTO part VALUES ('p','m','s',1,2,?)",
        ('{"type":"tool","tool":"bash","callID":"c","state":{"status":"completed",'
         '"input":{"command":"display clock"},"output":"ok",'
         '"metadata":{"exit":0}}}',),
    )
    conn.commit()
    conn.close()
    lines = session_archive_to_event_lines(path.read_bytes(), "run-wal")
    assert any('"tool_call"' in l for l in lines)
    assert any('"display clock"' in l for l in lines)
