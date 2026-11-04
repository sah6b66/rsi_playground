"""Frame parsers: OpenCode wiretap NDJSON and native pg_trace NDJSON (DD-07 §5.1)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent

__all__ = ["NdjsonEventParser", "OpenCodeWiretapParser"]

# Non-main-loop kinds filtered out (never llm_request; DD-07 §5.1).
_FILTERED_KINDS = frozenset({"title", "compaction", "generate"})


class OpenCodeWiretapParser:
    """Parse OpenCode wiretap NDJSON lines into TraceEvents (TU-031)."""

    def parse_line(
        self,
        line: str,
        run_id: str,
        task_id: str,
        lab_id: str,
        sut_id: str,
    ) -> TraceEvent | None:
        try:
            frame: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(frame, dict):
            return None

        ftype = frame.get("type")
        kind = frame.get("kind")
        if kind in _FILTERED_KINDS:
            return None

        data: dict[str, Any] | None = None
        if ftype == "assembled" and kind == "context":
            data = {
                "system": [s.get("text", "") for s in frame.get("system", [])],
                "messages": frame.get("messages", []),
                "tools": frame.get("tools", []),
            }
        elif ftype == "http" and kind == "primary":
            data = {"body": frame.get("body")}
        if data is None:
            return None

        try:
            return TraceEvent(
                run_id=run_id,
                task_id=task_id,
                lab_id=lab_id,
                sut_id=sut_id,
                seq=0,  # assigned by TraceCollector.ingest
                ts=str(frame.get("at", "")),
                event_type=EventType.LLM_REQUEST,
                model=frame.get("model"),
                data=data,
                duration_ms=None,
            )
        except ValidationError:
            return None


class NdjsonEventParser:
    """Parse native plugin/hook NDJSON lines (already TraceEvent-shaped)."""

    def parse_line(self, line: str, defaults: dict) -> TraceEvent | None:
        try:
            obj: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None
        for key, value in defaults.items():
            if obj.get(key) in (None, ""):
                obj[key] = value
        try:
            return TraceEvent(**obj)
        except ValidationError:
            return None
