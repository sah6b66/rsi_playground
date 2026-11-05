"""Unit tests for rsi.orchestrator.budget (DD-12 §4.3)."""

from pathlib import Path

import pytest

from rsi.core.errors import BudgetExceeded
from rsi.orchestrator.budget import BudgetController

pytestmark = pytest.mark.asyncio


def make(tmp_path: Path, scope_limits=None, global_limit=100000):
    return BudgetController(
        stop_file=tmp_path / "STOP",
        scope_limits=scope_limits or {"batch-1": 1000},
        global_limit=global_limit,
    )


async def test_scope_check_and_reserve_ok(tmp_path):
    bc = make(tmp_path)
    await bc.check_and_reserve("batch-1", 500)


async def test_scope_check_and_reserve_raises(tmp_path):
    bc = make(tmp_path)
    await bc.record("batch-1", tokens=800, cost_usd=0.1, wall_time_s=1.0)
    with pytest.raises(BudgetExceeded):
        await bc.check_and_reserve("batch-1", 500)
    # exactly at limit is fine
    await bc.check_and_reserve("batch-1", 200)


async def test_record_accumulates(tmp_path):
    bc = make(tmp_path)
    await bc.record("batch-1", tokens=100, cost_usd=0.01, wall_time_s=1.0)
    await bc.record("batch-1", tokens=50, cost_usd=0.02, wall_time_s=2.0)
    assert bc.usage("batch-1") == {
        "tokens": 150,
        "cost_usd": pytest.approx(0.03),
        "wall_time_s": pytest.approx(3.0),
    }


async def test_global_check_raises_and_creates_stop_file(tmp_path):
    bc = make(tmp_path, scope_limits={"a": 1000, "b": 1000}, global_limit=1500)
    await bc.record("a", tokens=800, cost_usd=0.0, wall_time_s=0.0)
    await bc.record("b", tokens=800, cost_usd=0.0, wall_time_s=0.0)
    with pytest.raises(BudgetExceeded):
        await bc.check_global()
    assert bc.check_stop_file() is True
    assert bc.stop_file.exists()


async def test_global_check_ok_under_limit(tmp_path):
    bc = make(tmp_path)
    await bc.record("batch-1", tokens=100, cost_usd=0.0, wall_time_s=0.0)
    await bc.check_global()
    assert not bc.stop_file.exists()


async def test_halt_marks_scope(tmp_path):
    bc = make(tmp_path)
    await bc.record("batch-1", tokens=10, cost_usd=0.0, wall_time_s=0.0)
    await bc.halt("batch-1")
    assert bc.is_halted("batch-1")
    # halted scope cannot reserve
    with pytest.raises(BudgetExceeded):
        await bc.check_and_reserve("batch-1", 1)
    # halted scopes excluded from global sum
    await bc.check_global()


async def test_unknown_scope_uses_no_scope_limit(tmp_path):
    bc = make(tmp_path)
    await bc.check_and_reserve("unknown", 10**9)
