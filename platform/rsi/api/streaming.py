"""SSE + WebSocket streaming endpoints (DD-13 §4.4)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from rsi.api.deps import get_current_user
from rsi.core.errors import AuthError
from rsi.orchestrator.event_bus import EventBus
from rsi.security.auth import Claims

router = APIRouter(prefix="/streams", tags=["streams"])

_SSE_HEARTBEAT_S = 15.0
_WS_TAIL_N = 50
# DD-13 §4.4: idle WS heartbeat interval and connection lifetime cap.
# Both overridable per deployment via the settings store
# (``ws.heartbeat_s`` / ``ws.max_seconds``) — the design doc fixes the 30s
# heartbeat cadence; the 1h lifetime cap is an anti-zombie-connection
# default (DD-13 does not define one).
_WS_HEARTBEAT_S = 30.0
_WS_MAX_LIFETIME_S = 3600.0


def _ws_setting(stores: dict, key: str, default: float) -> float:
    """Read a WS tuning float from the settings store (test injection)."""
    try:
        raw = stores.get("settings", {}).get(key, {}).get("value", default)
        return float(raw)
    except (AttributeError, TypeError, ValueError):
        return default


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
async def ws_trace(websocket: WebSocket, run_id: str, token: str = "") -> None:
    """Run trace stream: tail frame, then persistent append + heartbeat.

    帧契约（DD-13 §4.4）——``{"type":"tail","events":[<=50 条]}`` 首帧，
    之后 ``{"type":"append","events":[新事件],"cursor":int}`` 增量帧、
    空闲时 ``{"type":"ping","ts":...}`` 心跳（默认 30s，settings store 的
    ``ws.heartbeat_s`` 可覆盖）。连接生命周期上限默认 1h（``ws.max_seconds``
    可覆盖，防僵尸连接——设计文档未定义上限，此处为工程默认值）。
    增量帧为批量 events 格式（设计示例为单 event 的
    ``{"type":"append","event":…}``；实现取批量以减少空闲 tick 下的帧
    碎片，字段语义一致）。

    鉴权（§9.20，D-14）：``?token=`` 短时效 query token。文档未定义短时效
    token 的签发机制，实现暂复用 JWT（AuthService.verify，角色限 A/E/R），
    独立短时效签发通道待 S11。
    """
    auth = websocket.app.state.services.get("auth_service")
    if auth is None:
        await websocket.close(code=1011, reason="auth service unavailable")
        return
    try:
        claims = auth.verify(token)
        if claims.role not in {"admin", "agent_engineer", "researcher"}:
            raise AuthError("role not allowed on run streams")
    except Exception:
        await websocket.close(code=4401, reason="invalid or missing token")
        return
    await websocket.accept()
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
    # N-5 (DD-13 §4.4): persistent session after the tail frame —
    # incremental {"type":"append","events":[...]} frames for new trace
    # events plus {"type":"ping","ts":...} heartbeats while idle. Wake
    # sources: a bus event (cheap hint that *something* progressed), any
    # client message (keepalive / explicit poll request) or the heartbeat
    # timeout; on each wake the traces store (authoritative source) is
    # re-scanned from the cursor.
    bus = _bus(websocket)
    heartbeat = _ws_setting(
        websocket.app.state.stores, "ws.heartbeat_s", _WS_HEARTBEAT_S
    )
    max_lifetime = _ws_setting(
        websocket.app.state.stores, "ws.max_seconds", _WS_MAX_LIFETIME_S
    )
    cursor = (events[-1].get("seq", 0) + 1) if events else 0
    queue = await bus.subscribe()
    deadline = time.monotonic() + max_lifetime
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await websocket.close(
                    code=1000, reason="connection lifetime cap reached"
                )
                return
            recv_task = asyncio.ensure_future(websocket.receive_text())
            bus_task = asyncio.ensure_future(queue.get())
            done, _pending = await asyncio.wait(
                {recv_task, bus_task},
                timeout=min(heartbeat, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (recv_task, bus_task):
                if not task.done():
                    task.cancel()
            client_msg: str | None = None
            for task in done:
                if task is recv_task:
                    try:
                        client_msg = task.result()
                    except WebSocketDisconnect:
                        return
                    except Exception:  # noqa: BLE001 - transport failure
                        await websocket.close(code=1011)
                        return
            new_events = [
                e
                for e in sorted(
                    traces.get(run_id, []), key=lambda e: e.get("seq", 0)
                )
                if e.get("seq", 0) >= cursor
            ]
            if new_events:
                cursor = new_events[-1].get("seq", 0) + 1
                await websocket.send_json(
                    {
                        "type": "append",
                        "events": new_events,
                        "cursor": cursor,
                    }
                )
            elif client_msg is not None or not done:
                # reply to a client keepalive, or heartbeat ping on idle
                await websocket.send_json({"type": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001 - server-side failure: close socket
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001 - already closed
            pass
    finally:
        bus.unsubscribe(queue)
