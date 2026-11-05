"""Frame parsers: OpenCode wiretap NDJSON and native pg_trace NDJSON (DD-07 §5.1)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.log import get_logger

__all__ = ["NdjsonEventParser", "OpenCodeWiretapParser", "estimate_tokens"]

logger = get_logger(__name__)

# Non-main-loop kinds filtered out (never llm_request; DD-07 §5.1).
_FILTERED_KINDS = frozenset({"title", "compaction", "generate"})


def estimate_tokens(text: str) -> tuple[int, bool]:
    """Estimate token count from character count. Returns (count, is_estimated)."""
    if not text:
        return 0, False
    return len(text) // 4, True  # ~4 chars per token heuristic


def _message_text(message: Any) -> str:
    """Best-effort textual content of one wiretap message entry."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if content is not None:
            return json.dumps(content, ensure_ascii=False)
    return ""


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
            system = [s.get("text", "") for s in frame.get("system", [])]
            messages = frame.get("messages", [])
            data = {
                "system": system,
                "messages": messages,
                "tools": frame.get("tools", []),
            }
            # H8: no usage data in the frame -> estimate input tokens from the
            # system + message text (~4 chars/token) and mark them estimated.
            # C-3: when the frame carries real usage (e.g. the final
            # ``--format json`` frame), keep it verbatim with estimated=False
            # so measured usage overrides the estimate (DD-07 §5.2).
            frame_usage = frame.get("usage")
            if frame_usage is None:
                text = "\n".join(system) + "\n" + "\n".join(
                    _message_text(m) for m in messages
                )
                tokens, is_estimated = estimate_tokens(text)
                if is_estimated:
                    data["usage"] = {
                        "input_tokens": tokens,
                        "estimated": True,
                    }
            elif isinstance(frame_usage, dict):
                usage = dict(frame_usage)
                usage.setdefault("estimated", False)
                data["usage"] = usage
        elif ftype == "http" and kind == "primary":
            data = {"body": frame.get("body")}
            # C-2: protocol probing from the request body (DD-07 §5.1):
            # "messages" -> chat_completions, "input" -> responses;
            # anything else (or unparseable) leaves protocol unset.
            body = frame.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    body = None
            if isinstance(body, dict):
                if "messages" in body:
                    data["protocol"] = "chat_completions"
                elif "input" in body:
                    data["protocol"] = "responses"
        if data is None:
            return None

        try:
            event = TraceEvent(
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
            logger.debug("parse_line.validation_error", parser="wiretap")
            return None
        logger.debug(
            "parse_line.exit", parser="wiretap", ftype=ftype, kind=kind
        )
        return event


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
            logger.debug("parse_line.validation_error", parser="ndjson_event")
            return None
