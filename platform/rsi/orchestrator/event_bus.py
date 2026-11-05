"""In-process pub/sub event bus for SSE event sourcing (DD-12 §4.5)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from rsi.log import get_logger

_QUEUE_MAXSIZE = 256

logger = get_logger(__name__)


class EventBus:
    """In-process pub/sub for SSE event sourcing.

    Messages are fanned out to every subscriber queue. Queues are bounded
    (256); when full the oldest message is dropped so the newest survives
    (drop-old-keep-new).
    """

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []

    async def publish(
        self, object_type: str, object_id: str, from_: str, to: str
    ) -> None:
        """Publish ``{object_type}.status_changed`` to all subscribers."""
        logger.debug(
            "publish.entry",
            object_type=object_type,
            object_id=object_id,
            from_=from_,
            to=to,
        )
        msg = {
            "event": f"{object_type}.status_changed",
            "ts": time.time(),
            "data": {"object_id": object_id, "from": from_, "to": to},
        }
        self._fan_out(msg)

    async def publish_alert(
        self, severity: str, message: str, scope: str | None = None
    ) -> None:
        """Publish a ``system.alert`` event (F4 v1.1) for SSE toast/badge."""
        logger.debug(
            "publish_alert.entry", severity=severity, scope=scope,
            message=message[:200] if len(message) > 200 else message,
        )
        msg = {
            "event": "system.alert",
            "ts": time.time(),
            "data": {"severity": severity, "message": message, "scope": scope},
        }
        self._fan_out(msg)

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a subscriber queue receiving EventBusMessage dicts."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subs.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove a subscriber queue; unknown queues are ignored."""
        try:
            self._subs.remove(queue)
        except ValueError:
            pass

    def _fan_out(self, msg: dict[str, Any]) -> None:
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
