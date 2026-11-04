"""Unit tests for rsi.orchestrator.checkpoint (DD-12 §4.4)."""

import pytest

from rsi.orchestrator.checkpoint import CheckpointStore

pytestmark = pytest.mark.asyncio


async def test_save_and_resume():
    store = CheckpointStore()
    await store.save("batch-1", {"cursor": 3, "status": "RUNNING"})
    assert await store.resume("batch-1") == {"cursor": 3, "status": "RUNNING"}


async def test_resume_nonexistent_returns_none():
    store = CheckpointStore()
    assert await store.resume("nope") is None


async def test_save_overwrites():
    store = CheckpointStore()
    await store.save("scope", {"cursor": 1})
    await store.save("scope", {"cursor": 9})
    assert await store.resume("scope") == {"cursor": 9}


async def test_scopes_are_isolated():
    store = CheckpointStore()
    await store.save("a", {"cursor": 1})
    await store.save("b", {"cursor": 2})
    assert await store.resume("a") == {"cursor": 1}
    assert await store.resume("b") == {"cursor": 2}
