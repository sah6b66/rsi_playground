"""TraceCollector: ingest / finalize / WS hub / NDJSON tail (DD-07 §4-5).

P0 simplification: in-memory event buffers and one-shot file read (no
continuous tail loop); interfaces match DD-07 for later upgrade.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rsi.core.models import TraceEvent
from rsi.log import get_logger
from rsi.tracing.parsers import NdjsonEventParser, OpenCodeWiretapParser

if TYPE_CHECKING:
    from rsi.tracing.seq import SeqAllocator
    from rsi.tracing.store import TraceStore

__all__ = ["TraceCollector"]

# WS subscriber queue bound (DD-07 §7-5): drop-old-keep-new semantics.
SUBSCRIBER_QUEUE_MAX = 256

logger = get_logger(__name__)


class TraceCollector:
    def __init__(
        self,
        seq_alloc: SeqAllocator,
        store: TraceStore | None,
        persistence_dir: Path | None = None,
    ) -> None:
        self._seq_alloc = seq_alloc
        self._store = store
        self._persistence_dir = Path(persistence_dir) if persistence_dir else None
        self._events: dict[str, list[TraceEvent]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._finalized: set[str] = set()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._wiretap_parser = OpenCodeWiretapParser()
        self._event_parser = NdjsonEventParser()

    def attach_store(self, store: "TraceStore | None") -> None:
        """Wire the query-side store (collector<->store circular dependency).

        The production composition root builds the collector first (store
        optional), then the TraceStore over the collector, then calls this
        method so ``finalize`` meta flows into the query side — no private
        attribute poking (audit T2-2).
        """
        self._store = store

    # -- ingest -----------------------------------------------------------

    def ingest(self, events: list[TraceEvent]) -> None:
        """Assign seq, buffer per run, publish to WS subscribers."""
        logger.debug("ingest.entry", events=len(events))
        for ev in events:
            late = ev.run_id in self._finalized
            ev = ev.model_copy(
                update={
                    "seq": self._seq_alloc.next(ev.run_id),
                    "late": late or ev.late,
                }
            )
            self._events.setdefault(ev.run_id, []).append(ev)
            self._persist_event(ev)
            self._publish(ev.run_id, ev)

    def _persist_event(self, ev: TraceEvent) -> None:
        """H7: append each event as an NDJSON line (when persistence is on)."""
        if self._persistence_dir is None:
            return
        run_dir = self._persistence_dir / ev.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "events.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(ev.model_dump_json() + "\n")

    def _publish(self, run_id: str, ev: TraceEvent) -> None:
        for queue in self._subscribers.get(run_id, []):
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest, keep newest
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # racing producer; drop

    # -- finalize ---------------------------------------------------------

    def finalize(self, run_id: str) -> None:
        logger.debug("finalize.entry", run_id=run_id)
        self._finalized.add(run_id)
        events = self._events.get(run_id, [])
        tss = sorted(e.ts for e in events if e.ts)
        meta: dict[str, Any] = {
            "run_id": run_id,
            "event_count": len(events),
            "first_ts": tss[0] if tss else None,
            "last_ts": tss[-1] if tss else None,
            "finalized": True,
        }
        self._meta[run_id] = meta
        if self._persistence_dir is not None:
            # H7: persist run meta as {persistence_dir}/{run_id}/meta.json
            run_dir = self._persistence_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "event_count": meta["event_count"],
                        "first_ts": meta["first_ts"],
                        "last_ts": meta["last_ts"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if self._store is not None:
            self._store.set_meta(run_id, meta)
        # Close WS subscribers for the finalized run (DD-07 §4 finalize-5).
        self._subscribers.pop(run_id, None)
        logger.debug("finalize.exit", run_id=run_id, event_count=len(events))

    # -- WS hub -----------------------------------------------------------

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(run_id, [])
        if queue in queues:
            queues.remove(queue)

    # -- tail -------------------------------------------------------------

    def tail_ndjson(self, path: Path, run_id: str, defaults: dict) -> int:
        """One-shot read (P0): parse each line and ingest.

        Detects wiretap frames (type/kind fields) vs native TraceEvent
        lines (event_type field) and dispatches to the right parser.
        Returns the number of ingested events.
        """
        logger.debug("tail_ndjson.entry", path=str(path), run_id=run_id)
        ingested: list[TraceEvent] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    probe = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(probe, dict) and "event_type" in probe:
                    parsed = self._event_parser.parse_line(line, defaults)
                else:
                    parsed = self._wiretap_parser.parse_line(
                        line,
                        run_id,
                        defaults.get("task_id", ""),
                        defaults.get("lab_id", ""),
                        defaults.get("sut_id", ""),
                    )
                if parsed is not None:
                    ingested.append(parsed)
        if ingested:
            self.ingest(ingested)
        logger.debug("tail_ndjson.exit", ingested=len(ingested))
        return len(ingested)

    # -- meta access ------------------------------------------------------

    def get_meta(self, run_id: str) -> dict[str, Any] | None:
        return self._meta.get(run_id)

    def events(self, run_id: str) -> list[TraceEvent]:
        return list(self._events.get(run_id, []))

    def all_events(self) -> list[TraceEvent]:
        return [e for events in self._events.values() for e in events]
