"""Unit tests for rsi.tracing (Tasks 7+8, DD-07 §3-5).

Covers: SeqAllocator, OpenCodeWiretapParser, NdjsonEventParser,
TraceCollector (ingest/finalize/subscribe/tail_ndjson), TraceStore
(query/stats/pagination), plus an end-to-end wiretap file flow.
"""

import asyncio
import json

import pytest

from rsi.core.enums import EventType
from rsi.core.models import TraceEvent
from rsi.tracing.collector import TraceCollector
from rsi.tracing.parsers import NdjsonEventParser, OpenCodeWiretapParser
from rsi.tracing.seq import SeqAllocator
from rsi.tracing.store import TraceFilter, TraceStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def ev(**kw) -> TraceEvent:
    base = dict(
        run_id="run-1",
        task_id="fault/demo-01",
        lab_id="lab-1",
        sut_id="sut-a",
        seq=0,
        ts="2026-11-04T10:00:00.000Z",
        event_type=EventType.TOOL_CALL,
        data={},
    )
    base.update(kw)
    return TraceEvent(**base)


# ---------------------------------------------------------------------------
# 1. SeqAllocator
# ---------------------------------------------------------------------------


class TestSeqAllocator:
    def test_monotonic_per_run(self):
        alloc = SeqAllocator()
        assert alloc.next("r1") == 0
        assert alloc.next("r1") == 1
        assert alloc.next("r1") == 2

    def test_independent_across_runs(self):
        alloc = SeqAllocator()
        assert alloc.next("r1") == 0
        assert alloc.next("r2") == 0
        assert alloc.next("r1") == 1
        assert alloc.next("r2") == 1


# ---------------------------------------------------------------------------
# 2. OpenCodeWiretapParser
# ---------------------------------------------------------------------------


class TestWiretapParser:
    def setup_method(self):
        self.p = OpenCodeWiretapParser()

    def test_assembled_context(self):
        line = json.dumps(
            {
                "type": "assembled",
                "kind": "context",
                "at": "2026-11-04T10:00:01.500Z",
                "model": "gl-4.7",
                "system": [{"text": "You are a network SRE."}, {"text": "Be terse."}],
                "messages": [{"role": "user", "content": "ping fails"}],
                "tools": [{"name": "ssh_exec"}],
            }
        )
        e = self.p.parse_line(line, "run-1", "fault/demo-01", "lab-1", "sut-a")
        assert e is not None
        assert e.event_type == EventType.LLM_REQUEST
        assert e.ts == "2026-11-04T10:00:01.500Z"
        assert e.model == "gl-4.7"
        assert e.duration_ms is None
        assert e.data["system"] == ["You are a network SRE.", "Be terse."]
        assert e.data["messages"] == [{"role": "user", "content": "ping fails"}]
        assert e.data["tools"] == [{"name": "ssh_exec"}]

    def test_http_primary(self):
        line = json.dumps(
            {
                "type": "http",
                "kind": "primary",
                "at": "2026-11-04T10:00:02.000Z",
                "model": "gl-4.7",
                "body": {"model": "gl-4.7", "messages": [{"role": "user"}]},
            }
        )
        e = self.p.parse_line(line, "run-1", "fault/demo-01", "lab-1", "sut-a")
        assert e is not None
        assert e.event_type == EventType.LLM_REQUEST
        assert e.data["body"] == {"model": "gl-4.7", "messages": [{"role": "user"}]}

    @pytest.mark.parametrize("kind", ["title", "compaction", "generate"])
    @pytest.mark.parametrize("ftype", ["assembled", "http"])
    def test_filtered_kinds(self, kind, ftype):
        line = json.dumps({"type": ftype, "kind": kind, "at": "2026-11-04T10:00:03Z"})
        assert self.p.parse_line(line, "run-1", "t", "l", "s") is None

    def test_invalid_json(self):
        assert self.p.parse_line("{not json", "run-1", "t", "l", "s") is None

    def test_unknown_frame_type(self):
        line = json.dumps({"type": "other", "kind": "x", "at": "2026-11-04T10:00:03Z"})
        assert self.p.parse_line(line, "run-1", "t", "l", "s") is None


# ---------------------------------------------------------------------------
# 3. NdjsonEventParser
# ---------------------------------------------------------------------------


class TestNdjsonEventParser:
    def test_round_trip(self):
        p = NdjsonEventParser()
        original = ev(event_type=EventType.TOOL_RESULT, data={"cmd": "disp ip int b"})
        line = original.model_dump_json()
        e = p.parse_line(line, defaults={})
        assert e == original.model_copy(update={"seq": 0})

    def test_defaults_fill_missing_ids(self):
        p = NdjsonEventParser()
        line = json.dumps(
            {
                "run_id": "run-9",
                "task_id": "fault/x-01",
                "lab_id": "lab-x",
                "sut_id": "sut-x",
                "seq": 3,
                "ts": "2026-11-04T10:00:00.000Z",
                "event_type": "error",
                "data": {"msg": "boom"},
            }
        )
        e = p.parse_line(line, defaults={"run_id": "ignored", "task_id": "filled"})
        assert e.run_id == "run-9"
        assert e.task_id == "fault/x-01"

    def test_defaults_applied_when_missing(self):
        p = NdjsonEventParser()
        line = json.dumps(
            {
                "task_id": "fault/y-01",
                "lab_id": "lab-y",
                "sut_id": "sut-y",
                "seq": 0,
                "ts": "2026-11-04T10:00:00.000Z",
                "event_type": "task_end",
            }
        )
        e = p.parse_line(
            line, defaults={"run_id": "run-def", "task_id": "task-def"}
        )
        assert e.run_id == "run-def"
        assert e.task_id == "fault/y-01"

    def test_invalid_json(self):
        assert NdjsonEventParser().parse_line("nope", defaults={}) is None


# ---------------------------------------------------------------------------
# 4. TraceCollector
# ---------------------------------------------------------------------------


def make_collector():
    alloc = SeqAllocator()
    col = TraceCollector(alloc, store=None)
    return col, alloc


class TestCollector:
    def test_ingest_assigns_monotonic_seq(self):
        col, _ = make_collector()
        col.ingest([ev(data={"i": 1}), ev(data={"i": 2}), ev(run_id="run-2")])
        seqs = [e.seq for e in col._events["run-1"]]
        assert seqs == [0, 1]
        assert col._events["run-2"][0].seq == 0

    def test_finalize_writes_meta(self):
        col, _ = make_collector()
        col.ingest(
            [
                ev(ts="2026-11-04T10:00:00.000Z"),
                ev(ts="2026-11-04T10:00:05.000Z"),
                ev(ts="2026-11-04T10:00:03.000Z"),
            ]
        )
        col.finalize("run-1")
        assert "run-1" in col._finalized
        meta = col.get_meta("run-1")
        assert meta["event_count"] == 3
        assert meta["first_ts"] == "2026-11-04T10:00:00.000Z"
        assert meta["last_ts"] == "2026-11-04T10:00:05.000Z"

    def test_finalize_empty_run(self):
        col, _ = make_collector()
        col.finalize("run-none")
        meta = col.get_meta("run-none")
        assert meta["event_count"] == 0
        assert meta["first_ts"] is None

    def test_subscriber_receives_events(self):
        col, _ = make_collector()
        q = col.subscribe("run-1")
        col.ingest([ev(data={"i": 1})])
        got = q.get_nowait()
        assert got.data == {"i": 1}
        col.unsubscribe("run-1", q)
        col.ingest([ev(data={"i": 2})])
        assert q.empty()

    def test_subscriber_only_own_run(self):
        col, _ = make_collector()
        q1 = col.subscribe("run-1")
        col.ingest([ev(run_id="run-2")])
        assert q1.empty()

    def test_tail_ndjson_wiretap(self, tmp_path):
        col, _ = make_collector()
        path = tmp_path / "wiretap.ndjson"
        lines = [
            json.dumps(
                {
                    "type": "assembled",
                    "kind": "context",
                    "at": "2026-11-04T10:00:01.000Z",
                    "model": "m",
                    "system": [{"text": "s"}],
                    "messages": [],
                    "tools": [],
                }
            ),
            json.dumps({"type": "assembled", "kind": "title", "at": "x"}),
            "garbage",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        col.tail_ndjson(
            path,
            "run-1",
            defaults={
                "task_id": "fault/demo-01",
                "lab_id": "lab-1",
                "sut_id": "sut-a",
            },
        )
        assert len(col._events["run-1"]) == 1
        e = col._events["run-1"][0]
        assert e.event_type == EventType.LLM_REQUEST
        assert e.task_id == "fault/demo-01"


# ---------------------------------------------------------------------------
# 5. TraceStore
# ---------------------------------------------------------------------------


def make_store_with_data():
    col, _ = make_collector()
    store = TraceStore(col)
    col.ingest(
        [
            ev(seq=0, ts="2026-11-04T10:00:00.000Z", event_type=EventType.LLM_REQUEST, data={"usage": {"input_tokens": 100, "output_tokens": 50}}, duration_ms=1000),
            ev(seq=1, ts="2026-11-04T10:00:01.000Z", event_type=EventType.TOOL_CALL, duration_ms=500),
            ev(seq=2, ts="2026-11-04T10:00:02.000Z", event_type=EventType.ERROR, data={"msg": "x"}),
            ev(seq=0, run_id="run-2", ts="2026-11-04T10:00:03.000Z", event_type=EventType.TOOL_CALL),
        ]
    )
    return store


class TestStore:
    def test_query_filter_run_id(self):
        store = make_store_with_data()
        events, next_cursor = store.query(TraceFilter(run_ids=["run-1"]), None, 100)
        assert len(events) == 3
        assert all(e.run_id == "run-1" for e in events)
        assert next_cursor is None

    def test_query_filter_event_type(self):
        store = make_store_with_data()
        events, _ = store.query(
            TraceFilter(event_types=[EventType.TOOL_CALL]), None, 100
        )
        assert len(events) == 2

    def test_query_filter_is_error(self):
        store = make_store_with_data()
        events, _ = store.query(TraceFilter(is_error=True), None, 100)
        assert len(events) == 1
        assert events[0].event_type == EventType.ERROR
        events, _ = store.query(TraceFilter(is_error=False), None, 100)
        assert len(events) == 3

    def test_query_filter_task_id_and_ts(self):
        store = make_store_with_data()
        events, _ = store.query(TraceFilter(task_id="fault/demo-01"), None, 100)
        assert len(events) == 4
        events, _ = store.query(
            TraceFilter(
                from_ts="2026-11-04T10:00:00.001Z",
                to_ts="2026-11-04T10:00:02.500Z",
            ),
            None,
            100,
        )
        assert len(events) == 2

    def test_pagination(self):
        store = make_store_with_data()
        page1, cursor = store.query(TraceFilter(run_ids=["run-1"]), None, 2)
        assert len(page1) == 2
        assert cursor is not None
        page2, cursor2 = store.query(TraceFilter(run_ids=["run-1"]), cursor, 2)
        assert len(page2) == 1
        assert cursor2 is None
        assert [e.seq for e in page1 + page2] == [0, 1, 2]

    def test_stats(self):
        store = make_store_with_data()
        s = store.stats(TraceFilter(run_ids=["run-1"]))
        assert s["event_count"] == 3
        assert s["tokens"] == 150
        assert s["steps"] == 1  # tool_call count
        assert s["duration_ms"] == 1500

    def test_get_meta_missing(self):
        store = make_store_with_data()
        assert store.get_meta("nope") is None


# ---------------------------------------------------------------------------
# 6. End-to-end: wiretap file -> tail -> query
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_wiretap_file_to_query(self, tmp_path):
        alloc = SeqAllocator()
        col = TraceCollector(alloc, store=None)
        store = TraceStore(col)
        path = tmp_path / "wiretap.ndjson"
        frames = [
            {
                "type": "assembled",
                "kind": "context",
                "at": "2026-11-04T10:00:01.000Z",
                "model": "gl-4.7",
                "system": [{"text": "sys"}],
                "messages": [{"role": "user", "content": "go"}],
                "tools": [{"name": "t"}],
            },
            {
                "type": "http",
                "kind": "primary",
                "at": "2026-11-04T10:00:02.000Z",
                "model": "gl-4.7",
                "body": {"messages": [{"role": "user"}]},
            },
            {"type": "assembled", "kind": "compaction", "at": "2026-11-04T10:00:03Z"},
        ]
        path.write_text(
            "\n".join(json.dumps(f) for f in frames) + "\n", encoding="utf-8"
        )
        col.tail_ndjson(
            path,
            "run-e2e",
            defaults={
                "task_id": "fault/demo-01",
                "lab_id": "lab-1",
                "sut_id": "sut-a",
            },
        )
        col.finalize("run-e2e")

        events, cursor = store.query(TraceFilter(run_ids=["run-e2e"]), None, 100)
        assert cursor is None
        assert [e.seq for e in events] == [0, 1]
        assert all(e.event_type == EventType.LLM_REQUEST for e in events)
        assert events[0].model == "gl-4.7"

        meta = store.get_meta("run-e2e")
        assert meta["event_count"] == 2
        assert meta["first_ts"] == "2026-11-04T10:00:01.000Z"
        assert meta["last_ts"] == "2026-11-04T10:00:02.000Z"
