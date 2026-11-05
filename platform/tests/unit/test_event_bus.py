"""Unit tests for rsi.orchestrator.event_bus (DD-12 §4.5)."""

import asyncio

import pytest

from rsi.orchestrator.event_bus import EventBus

pytestmark = pytest.mark.asyncio


async def test_publish_delivers_to_subscriber():
    bus = EventBus()
    q = await bus.subscribe()
    await bus.publish("task", "T1", "RUNNING", "VERIFYING")
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert msg["event"] == "task.status_changed"
    assert "ts" in msg
    assert msg["data"] == {"object_id": "T1", "from": "RUNNING", "to": "VERIFYING"}


async def test_publish_fans_out_to_all_subscribers():
    bus = EventBus()
    q1 = await bus.subscribe()
    q2 = await bus.subscribe()
    await bus.publish("batch", "B1", "RUNNING", "DONE")
    for q in (q1, q2):
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "batch.status_changed"


async def test_publish_alert():
    bus = EventBus()
    q = await bus.subscribe()
    await bus.publish_alert("critical", "budget blown", scope="batch-1")
    msg = await asyncio.wait_for(q.get(), timeout=1)
    assert msg["event"] == "system.alert"
    assert msg["data"] == {"severity": "critical", "message": "budget blown", "scope": "batch-1"}
    await bus.publish_alert("info", "hello")
    msg2 = await asyncio.wait_for(q.get(), timeout=1)
    assert msg2["data"]["scope"] is None


async def test_bounded_queue_drops_old_keeps_new():
    bus = EventBus()
    q = await bus.subscribe()
    for i in range(300):
        await bus.publish("task", f"T{i}", "a", "b")
    assert q.qsize() == 256
    first = q.get_nowait()
    # oldest 44 dropped; queue starts at T44
    assert first["data"]["object_id"] == "T44"
    last = q.get_nowait()
    while not q.empty():
        last = q.get_nowait()
    assert last["data"]["object_id"] == "T299"


async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = await bus.subscribe()
    bus.unsubscribe(q)
    await bus.publish("task", "T1", "a", "b")
    await asyncio.sleep(0)
    assert q.empty()


async def test_unsubscribe_unknown_queue_is_noop():
    bus = EventBus()
    bus.unsubscribe(asyncio.Queue())
