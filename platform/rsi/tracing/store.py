"""TraceStore: query / stats / meta over collected TraceEvents (DD-07 §4).

P0: in-memory storage backed by TraceCollector buffers; the (run_id, seq)
cursor pagination contract matches the DD-07 interface for a later
file/index-backed implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.log import get_logger

if TYPE_CHECKING:
    from rsi.tracing.collector import TraceCollector

__all__ = ["TraceFilter", "TraceStore"]

logger = get_logger(__name__)


class TraceFilter(BaseModel):
    run_ids: list[str] | None = None
    task_id: str | None = None
    event_types: list[EventType] | None = None
    is_error: bool | None = None
    from_ts: str | None = None
    to_ts: str | None = None


class TraceStore:
    def __init__(self, collector: TraceCollector) -> None:
        self._collector = collector
        self._meta: dict[str, dict[str, Any]] = {}

    # -- write path (called by collector.finalize) ------------------------

    def set_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        self._meta[run_id] = meta

    def get_meta(self, run_id: str) -> dict[str, Any] | None:
        if run_id in self._meta:
            return self._meta[run_id]
        return self._collector.get_meta(run_id)

    # -- query ------------------------------------------------------------

    def query(
        self,
        filter: TraceFilter,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[TraceEvent], str | None]:
        logger.debug("query.entry", cursor=cursor, limit=limit)
        matched = self._filter_events(filter)
        if cursor is not None:
            cur_run, _, cur_seq = cursor.partition(":")
            cur_seq_n = int(cur_seq)
            matched = [
                e
                for e in matched
                if (e.run_id, e.seq) > (cur_run, cur_seq_n)
            ]
        page = matched[:limit]
        next_cursor: str | None = None
        if limit and len(matched) > limit and page:
            last = page[-1]
            next_cursor = f"{last.run_id}:{last.seq}"
        logger.debug("query.exit", matched=len(matched), page=len(page))
        return page, next_cursor

    def _filter_events(self, filter: TraceFilter) -> list[TraceEvent]:
        result: list[TraceEvent] = []
        for ev in self._collector.all_events():
            if filter.run_ids is not None and ev.run_id not in filter.run_ids:
                continue
            if filter.task_id is not None and ev.task_id != filter.task_id:
                continue
            if (
                filter.event_types is not None
                and ev.event_type not in filter.event_types
            ):
                continue
            if filter.is_error is not None:
                is_err = ev.event_type == EventType.ERROR
                if is_err != filter.is_error:
                    continue
            if filter.from_ts is not None and ev.ts < filter.from_ts:
                continue
            if filter.to_ts is not None and ev.ts > filter.to_ts:
                continue
            result.append(ev)
        # (run_id, seq) ordering: pages are globally ordered and cursors
        # are stable.
        result.sort(key=lambda e: (e.run_id, e.seq))
        return result

    # -- stats ------------------------------------------------------------

    def stats(self, filter: TraceFilter) -> dict[str, Any]:
        logger.debug("stats.entry")
        events = self._filter_events(filter)
        tokens = 0
        for ev in events:
            usage = ev.data.get("usage")
            if isinstance(usage, dict):
                tokens += int(usage.get("input_tokens", 0)) + int(
                    usage.get("output_tokens", 0)
                )
        steps = sum(1 for e in events if e.event_type == EventType.TOOL_CALL)
        duration_ms = sum(e.duration_ms or 0 for e in events)
        tss = sorted(e.ts for e in events if e.ts)
        logger.debug("stats.exit", event_count=len(events), tokens=tokens)
        return {
            "event_count": len(events),
            "tokens": tokens,
            "steps": steps,
            "duration_ms": duration_ms,
            "first_ts": tss[0] if tss else None,
            "last_ts": tss[-1] if tss else None,
        }
