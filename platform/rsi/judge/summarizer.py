"""TraceSummarizer — per-event_type grouped trace summary (DD-08 §5.1 note 3).

Compresses raw trace events into a human-readable, budget-bounded prompt
fragment. Each event becomes one line ``[<seq>] <event_type> <summary>``;
when the estimated token count (``len // 4``) exceeds the budget, the oldest
event lines are merged into a single compressed-count line until the summary
fits.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rsi.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from rsi.core.models import TraceEvent

__all__ = ["TraceSummarizer"]

logger = get_logger(__name__)

_ARGS_MAX = 200
_SYSTEM_MAX = 200


def _trunc(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _dump(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(obj)


class TraceSummarizer:
    """Grouped trace summarizer for LLM judge prompts (M4)."""

    def summarize(self, events: "list[TraceEvent]", budget_tokens: int = 8000) -> str:
        if not events:
            return ""
        lines = [self._event_line(e) for e in events]
        text = "\n".join(lines)
        # Compress oldest events into one counted line until within budget.
        while len(lines) > 1 and len(text) // 4 > budget_tokens:
            lines = self._compress_one(lines)
            text = "\n".join(lines)
        return text

    # -- internals ----------------------------------------------------------------

    def _event_line(self, e: "TraceEvent") -> str:
        et = str(e.event_type.value)
        data = e.data or {}
        if et == "tool_call":
            # 兼容两种事件形态：wiretap（name/args）与会话存档转换
            # （tool/command/input）——命令文本必须可见，否则 LLM 裁判
            # 无法核证 critical_steps（E2 实测误判 skipped）。
            name = str(data.get("name") or data.get("tool") or "")
            raw_args = (
                data.get("args")
                if data.get("args") not in (None, "")
                else data.get("command")
                if data.get("command")
                else data.get("input")
            )
            args = _dump(raw_args or "")[:_ARGS_MAX]
            return f"[{e.seq}] {et} name={name} args={args}"
        if et == "llm_request":
            system = str(data.get("system", ""))[:_SYSTEM_MAX]
            msgs = data.get("messages", [])
            n = len(msgs) if isinstance(msgs, list) else 0
            return f"[{e.seq}] {et} system={system} messages={n}"
        if et == "llm_response":
            finish = data.get("finish_reason", "")
            usage = _dump(data.get("usage", {}))
            return f"[{e.seq}] {et} finish_reason={finish} usage={usage}"
        keys = ",".join(sorted(k for k in data.keys() if isinstance(k, str)))
        return f"[{e.seq}] {et} keys=[{keys}]"

    @staticmethod
    def _compress_one(lines: list[str]) -> list[str]:
        """Merge the first line into (or create) a leading compressed-count line."""
        counts: dict[str, int] = {}

        def add(line: str) -> None:
            parts = line.split(" ", 2)
            if len(parts) >= 2:
                counts[parts[1]] = counts.get(parts[1], 0) + 1

        first = lines[0]
        if first.startswith("(") and " older events compressed: " in first:
            head, _, detail = first[1:-1].partition(" older events compressed: ")
            for chunk in detail.split(", "):
                name, _, cnt = chunk.partition("×")
                if cnt.isdigit():
                    counts[name] = counts.get(name, 0) + int(cnt)
            add(lines[1])
            rest = lines[2:]
        else:
            add(first)
            rest = lines[1:]
        summary = ", ".join(f"{k}×{v}" for k, v in counts.items())
        total = sum(counts.values())
        return [f"({total} older events compressed: {summary})", *rest]
