"""SSE + WebSocket streaming endpoints (DD-13 §4.4)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from rsi.api.deps import get_current_user
from rsi.orchestrator.event_bus import EventBus
from rsi.security.auth import Claims

router = APIRouter(prefix="/streams", tags=["streams"])

_SSE_HEARTBEAT_S = 15.0
_WS_TAIL_N = 50


def _bus(request: Request) -> EventBus:
    """Resolve the EventBus service, creating one lazily for P0 setups."""
    bus = request.app.state.services.get("event_bus")
    if bus is None:
        bus = getattr(request.app.state, "event_bus", None)
        if bus is None:
            bus = EventBus()
            request.app.state.event_bus = bus
    return bus


async def _sse_stream(bus: EventBus, max_seconds: float) -> AsyncIterator[str]:
    queue = await bus.subscribe()
    deadline = time.monotonic() + max_seconds
    try:
        open_msg = {
            "event": "stream.open",
            "ts": time.time(),
            "data": {"note": "subscribed to EventBus"},
        }
        yield f"data: {json.dumps(open_msg)}\n\n"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                msg = await asyncio.wait_for(
                    queue.get(), timeout=min(_SSE_HEARTBEAT_S, remaining)
                )
                yield f"data: {json.dumps(msg)}\n\n"
            except asyncio.TimeoutError:
                yield ": ping\n\n"
    finally:
        bus.unsubscribe(queue)


@router.get("/events")
async def sse_events(
    request: Request,
    user: Claims = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events stream: ``*.status_changed`` + ``system.alert``."""
    bus = _bus(request)
    settings = request.app.state.stores["settings"]
    max_seconds = float(
        settings.get("sse.max_seconds", {}).get("value", 3600)
    )
    return StreamingResponse(
        _sse_stream(bus, max_seconds), media_type="text/event-stream"
    )


@router.websocket("/runs/{run_id}")
async def ws_trace(websocket: WebSocket, run_id: str) -> None:
    """Trace tail stream. P0: accept, send tail, send ping, close."""
    await websocket.accept()
    try:
        traces: dict[str, list[dict[str, Any]]] = websocket.app.state.stores[
            "traces"
        ]
        events = sorted(
            traces.get(run_id, []), key=lambda e: e.get("seq", 0)
        )[-_WS_TAIL_N:]
        if not events and run_id not in websocket.app.state.stores["runs"]:
            await websocket.send_json(
                {"type": "error", "error": f"run {run_id!r} not found"}
            )
            await websocket.close()
            return
        await websocket.send_json({"type": "tail", "events": events})
        await websocket.send_json({"type": "ping"})
        await websocket.close()
    except WebSocketDisconnect:
        return
