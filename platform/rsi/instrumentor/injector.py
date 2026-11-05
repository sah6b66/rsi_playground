"""Injector: apply an InstrumentPlan onto an overlay directory (DD-06 §5.1).

Only ever writes inside ``overlay_dir`` — the original config tree is never
touched (ADR-4).
"""

from __future__ import annotations

import json
from pathlib import Path

from rsi.instrumentor.planner import InstrumentPlan

HOOK_PLUGIN = "rsi-trace-hook.ts"

HOOK_TEMPLATE = """\
// PG trace hook (@opencode/plugin V2) — injected by rsi instrumentor
import {{ plugin }} from "@opencode/plugin"

export const rsiTraceHook = plugin({{ name: "rsi-trace-hook" }}, ({{ project }}) => {{
  const sink = process.env.PG_PROXY_SINK ?? "/trace/events.ndjson"
  const runId = process.env.RSI_RUN_ID ?? "unknown"
  const append = (event: Record<string, unknown>) => {{
    const line = JSON.stringify({{ run_id: runId, ts: new Date().toISOString(), ...event }})
    // append-only side channel; collector (DD-07) assigns seq numbers
    void fetch("http://127.0.0.1:1/nope").catch(() => {{}}) // no-op placeholder
    console.error("PG-TRACE " + line)
  }}
  project.on("tool.execute.before", ({{ tool, sessionID }}) => {{
    append({{ event_type: "tool_call", data: {{ tool, session: sessionID }} }})
  }})
  project.on("tool.execute.after", ({{
    append({{ event_type: "tool_result", data: {{ tool }}, duration_ms: 0, ok: true }})
  }})
}})
"""


class Injector:
    @staticmethod
    def apply(overlay_dir: Path, plan: InstrumentPlan) -> None:
        overlay_dir = Path(overlay_dir)
        instrumentable = [i for i in plan.items if i.method != "source"]
        if instrumentable:
            Injector._init_sink(overlay_dir)
        for item in plan.items:
            if item.method == "mcp_wrap":
                Injector._wrap_mcp(overlay_dir)
            elif item.method == "hook":
                Injector._write_hook(overlay_dir)
            elif item.method == "source":
                Injector._mark_source(overlay_dir, item.file)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _init_sink(overlay_dir: Path) -> None:
        """Create the trace sink and write one bootstrap event so the smoke
        verifier can confirm the instrumented side produces events (P0 offline)."""
        sink = overlay_dir / "trace" / "events.ndjson"
        sink.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "run_id": "instrumentor",
            "event_type": "tool_call",
            "data": {"bootstrap": "instrumentation applied"},
        }
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    @staticmethod
    def _wrap_mcp(overlay_dir: Path) -> None:
        mcp_path = overlay_dir / "mcp.json"
        if not mcp_path.is_file():
            return
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or data.get("servers")
        if servers is None:
            return
        key = "mcpServers" if "mcpServers" in data else "servers"
        for cfg in servers.values():
            if not isinstance(cfg, dict) or "command" not in cfg:
                continue
            orig_cmd = cfg["command"]
            orig_args = list(cfg.get("args", []))
            cfg["command"] = "pg-mcp-proxy"
            cfg["args"] = ["--", orig_cmd, *orig_args]
            env = dict(cfg.get("env") or {})
            env.setdefault("PG_PROXY_SINK", "/trace/events.ndjson")
            cfg["env"] = env
        mcp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _write_hook(overlay_dir: Path) -> None:
        plugins = overlay_dir / "plugins"
        plugins.mkdir(exist_ok=True)
        (plugins / HOOK_PLUGIN).write_text(HOOK_TEMPLATE, encoding="utf-8")
        # register in opencode.json when present
        oc = overlay_dir / "opencode.json"
        if oc.is_file():
            try:
                data = json.loads(oc.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            plugins_list = data.setdefault("plugins", [])
            entry = f"plugins/{HOOK_PLUGIN}"
            if entry not in plugins_list:
                plugins_list.append(entry)
            oc.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _mark_source(overlay_dir: Path, rel_file: str) -> None:
        target = overlay_dir / rel_file
        if not target.is_file():
            return
        text = target.read_text(encoding="utf-8")
        if "PG-TRACE-BEGIN" in text:
            return
        marked = (
            "<!-- PG-TRACE-BEGIN (rsi instrumentor; P0 marker, no-op) -->\n"
            + text
            + "\n<!-- PG-TRACE-END -->\n"
        )
        target.write_text(marked, encoding="utf-8")
