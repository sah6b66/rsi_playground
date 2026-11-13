"""Trace endpoints (DD-13 §9.8): diff + query + stats. P0: in-memory traces."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from rsi.api.db_reads import get_run_record
from rsi.api.deps import get_current_user, get_store
from rsi.core.errors import NotFoundError
from rsi.security.auth import Claims

router = APIRouter(tags=["traces"])


def _get_run(store: dict, run_id: str) -> dict:
    try:
        return store[run_id]
    except KeyError:
        raise NotFoundError(
            f"run {run_id!r} not found", context={"run_id": run_id}
        ) from None


class TraceDiffOut(BaseModel):
    run_a: str
    run_b: str
    by_type: dict[str, list[int]]  # event type -> [count_a, count_b]
    only_in_a: list[dict[str, Any]]
    only_in_b: list[dict[str, Any]]


class TraceQueryBody(BaseModel):
    model_config = {"extra": "allow"}

    run_id: str | None = None
    type: str | None = None
    since_seq: int | None = None


class TraceQueryOut(BaseModel):
    items: list[dict[str, Any]]


class TraceStatsBody(BaseModel):
    run_id: str | None = None


class TraceStatsOut(BaseModel):
    total_events: int
    by_type: dict[str, int]
    run_ids: list[str]


def _collector_events(request: Request, run_id: str | None) -> list[dict[str, Any]]:
    """F2/W12：collector（trace_store）是生产路径唯一真实事件源——内存
    traces store 无此 run 时回退读 collector。"""
    trace_store = request.app.state.services.get("trace_store")
    if trace_store is None:
        return []
    try:
        from rsi.tracing.store import TraceFilter

        events, _cursor = trace_store.query(
            TraceFilter(run_ids=[run_id] if run_id else None), None, 10_000
        )
    except Exception:  # noqa: BLE001 - 回退失败按无事件处理
        return []
    out: list[dict[str, Any]] = []
    for ev in events:
        d = ev.model_dump(mode="json")
        d.setdefault("type", d.get("event_type"))
        out.append(d)
    return out


def _events(request: Request, traces: dict, run_id: str | None) -> list[dict[str, Any]]:
    if run_id is not None:
        if traces.get(run_id):
            return sorted(traces.get(run_id, []), key=lambda e: e["seq"])
        return sorted(_collector_events(request, run_id), key=lambda e: e["seq"])
    out: list[dict[str, Any]] = []
    for rid in traces:
        out.extend({**e, "run_id": rid} for e in traces[rid])
    out.extend(e for e in _collector_events(request, None) if not traces.get(e.get("run_id", "")))
    return sorted(out, key=lambda e: e.get("seq", 0))


@router.get("/traces/diff", response_model=TraceDiffOut)
async def diff_traces(
    run_a: str,
    run_b: str,
    request: Request,
    user: Claims = Depends(get_current_user),
    runs: dict = Depends(get_store("runs")),
    traces: dict = Depends(get_store("traces")),
) -> TraceDiffOut:
    # audit4 W-C: run existence falls back to the DB (memory store is
    # test-only in production assemblies).
    for rid in (run_a, run_b):
        if get_run_record(request, rid, runs) is None:
            _get_run(runs, rid)  # raise the canonical 404
    ev_a = sorted(traces.get(run_a, []), key=lambda e: e["seq"])
    ev_b = sorted(traces.get(run_b, []), key=lambda e: e["seq"])
    types = sorted({e["type"] for e in ev_a} | {e["type"] for e in ev_b})
    by_type = {
        t: [
            sum(1 for e in ev_a if e["type"] == t),
            sum(1 for e in ev_b if e["type"] == t),
        ]
        for t in types
    }
    seq_b = {e["seq"] for e in ev_b}
    seq_a = {e["seq"] for e in ev_a}
    return TraceDiffOut(
        run_a=run_a,
        run_b=run_b,
        by_type=by_type,
        only_in_a=[e for e in ev_a if e["seq"] not in seq_b],
        only_in_b=[e for e in ev_b if e["seq"] not in seq_a],
    )


@router.post("/traces:query", response_model=TraceQueryOut)
async def query_traces(
    body: TraceQueryBody,
    request: Request,
    user: Claims = Depends(get_current_user),
    traces: dict = Depends(get_store("traces")),
) -> TraceQueryOut:
    events = _events(request, traces, body.run_id)
    if body.type is not None:
        events = [e for e in events if e.get("type") == body.type]
    if body.since_seq is not None:
        events = [e for e in events if e.get("seq", 0) >= body.since_seq]
    return TraceQueryOut(items=events)


@router.post("/traces:stats", response_model=TraceStatsOut)
async def trace_stats(
    body: TraceStatsBody,
    request: Request,
    user: Claims = Depends(get_current_user),
    traces: dict = Depends(get_store("traces")),
) -> TraceStatsOut:
    events = _events(request, traces, body.run_id)
    run_ids = set(traces.keys())
    run_ids.update(e.get("run_id") for e in _collector_events(request, None) if e.get("run_id"))
    return TraceStatsOut(
        total_events=len(events),
        by_type=dict(Counter(e.get("type", "unknown") for e in events)),
        run_ids=sorted(r for r in run_ids if r),
    )
