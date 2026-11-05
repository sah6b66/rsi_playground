"""B-11: pg-mcp-proxy.mjs subprocess contract test (DD-06 §5.3 / IN-005).

Spawns the proxy around an echo MCP server (stdio JSON-RPC) and asserts:
- responses pass through,
- the sink receives tool_call + tool_result events,
- tool_result carries duration_ms and is paired to the right request id.

Skipped when node is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

PROXY = Path(__file__).resolve().parents[2].parent / "deploy" / "vendor" / "pg-mcp-proxy.mjs"

ECHO_SERVER = """\
const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let msg; try { msg = JSON.parse(line); } catch (e) { return; }
  if (msg.id === undefined) return;
  let result;
  if (msg.method === 'initialize') {
    result = { protocolVersion: '2024-11-05', capabilities: {}, serverInfo: { name: 'echo' } };
  } else if (msg.method === 'tools/list') {
    result = { tools: [{ name: 'echo', description: 'echo' }] };
  } else if (msg.method === 'tools/call') {
    result = { content: [{ type: 'text', text: 'echo:' + JSON.stringify(msg.params || {}) }] };
  } else {
    result = {};
  }
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result }) + '\\n');
});
"""

has_node = shutil.which("node") is not None and PROXY.is_file()

pytestmark = pytest.mark.skipif(not has_node, reason="node or proxy script unavailable")


def _read_json_line(proc: subprocess.Popen, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    buf = ""
    while time.monotonic() < deadline:
        char = proc.stdout.read(1)
        if char:
            buf += char
            if buf.endswith("\n"):
                return json.loads(buf.strip())
        else:
            time.sleep(0.02)
    raise TimeoutError(f"no JSON line from proxy within {timeout_s}s (buf={buf!r})")


def test_proxy_pairs_events_and_passes_through(tmp_path: Path) -> None:
    server = tmp_path / "echo-server.cjs"
    server.write_text(ECHO_SERVER, encoding="utf-8")
    sink = tmp_path / "events.ndjson"

    env = {**__import__("os").environ, "PG_PROXY_SINK": str(sink), "RSI_RUN_ID": "run-b11"}
    proc = subprocess.Popen(
        ["node", str(PROXY), "--", "node", str(server)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        # initialize (id 1) then tools/call (id 2)
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ) + "\n")
        proc.stdin.flush()
        init_resp = _read_json_line(proc)
        assert init_resp["id"] == 1

        call = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "echo", "arguments": {"hello": "world"}},
        }
        proc.stdin.write(json.dumps(call) + "\n")
        proc.stdin.flush()
        call_resp = _read_json_line(proc)
        # passthrough: the echo server's result content reached the client
        assert call_resp["id"] == 2
        assert "echo:" in call_resp["result"]["content"][0]["text"]

        # give the proxy a moment to flush sink writes
        for _ in range(50):
            if sink.exists() and len(sink.read_text(encoding="utf-8").splitlines()) >= 2:
                break
            time.sleep(0.05)
        events = [
            json.loads(line)
            for line in sink.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        by_type = {e["event_type"]: e for e in events}
        assert "tool_call" in by_type
        assert by_type["tool_call"]["data"]["name"] == "echo"
        assert "tool_result" in by_type
        tr = by_type["tool_result"]
        assert tr["data"]["tool"] == "echo"  # paired by id -> right tool name
        assert isinstance(tr["duration_ms"], int) and tr["duration_ms"] >= 0
        assert tr["run_id"] == "run-b11"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
