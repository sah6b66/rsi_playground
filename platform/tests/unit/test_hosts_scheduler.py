"""Unit tests: HostPool + InstanceScheduler (DD-05 §4.1, R1/R2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from rsi.carrier.hosts import HostPool
from rsi.carrier.scheduler import InstanceScheduler
from rsi.core.errors import CarrierError

CONFIGS: dict[str, dict] = {
    "main": {"docker": "local"},
    "remote-1": {"docker": "ssh://mock@host1", "max_instances": 2},
}


def _pool(**configs: dict) -> HostPool:
    return HostPool(configs or CONFIGS)


def _mock_client(pool: HostPool, reachable: dict[str, bool]) -> None:
    cache: dict[str, MagicMock] = {}

    def fake_client(host: str) -> MagicMock:
        if host not in cache:
            c = MagicMock()
            if reachable.get(host, True):
                c.ping.return_value = True
            else:
                c.ping.side_effect = ConnectionError("no daemon")
            cache[host] = c
        return cache[host]

    pool.client = MagicMock(side_effect=fake_client)  # type: ignore[method-assign]


class TestHostPoolConfig:
    def test_configs_parsed(self) -> None:
        pool = _pool()
        cfg = pool.config("remote-1")
        assert cfg.docker == "ssh://mock@host1"
        assert cfg.max_instances == 2
        assert cfg.subnets == []

    def test_unknown_host_config_raises(self) -> None:
        with pytest.raises(CarrierError):
            _pool().config("nope")

    def test_client_cached_per_host(self) -> None:
        pool = _pool()
        _mock_client(pool, {})
        assert pool.client("main") is pool.client("main")
        assert pool.client("main") is not pool.client("remote-1")

    def test_client_unknown_host_raises(self) -> None:
        with pytest.raises(CarrierError):
            _pool().client("nope")

    def test_default_offline_stub(self) -> None:
        pool = HostPool()
        assert pool.client("main") is None
        assert pool.check_reach("main") is True


class TestHostPoolReach:
    def test_check_reach_true(self) -> None:
        pool = _pool()
        _mock_client(pool, {"main": True})
        assert pool.check_reach("main") is True

    def test_check_reach_false(self) -> None:
        pool = _pool()
        _mock_client(pool, {"remote-1": False})
        assert pool.check_reach("remote-1") is False

    def test_check_reach_unknown_false(self) -> None:
        assert _pool().check_reach("nope") is False

    def test_statuses_lists_all(self) -> None:
        pool = _pool()
        _mock_client(pool, {"main": True, "remote-1": False})
        st = {s.host: s for s in pool.statuses()}
        assert set(st) == {"main", "remote-1"}
        assert st["main"].docker_url == "local"
        assert st["main"].reachable is True
        assert st["remote-1"].docker_url == "ssh://mock@host1"
        assert st["remote-1"].reachable is False
        assert st["remote-1"].max_instances == 2
        assert st["main"].instance_count == 0


class TestHostPoolCounts:
    def test_increment_decrement(self) -> None:
        pool = _pool()
        pool.increment("main")
        pool.increment("main")
        pool.increment("remote-1")
        assert pool._instance_counts == {"main": 2, "remote-1": 1}
        pool.decrement("main")
        assert pool._instance_counts["main"] == 1

    def test_decrement_floor_zero(self) -> None:
        pool = _pool()
        pool.decrement("main")
        assert pool._instance_counts["main"] == 0

    def test_increment_unknown_raises(self) -> None:
        with pytest.raises(CarrierError):
            _pool().increment("nope")

    def test_counts_surface_in_statuses(self) -> None:
        pool = _pool()
        pool.increment("main")
        st = {s.host: s for s in pool.statuses()}
        assert st["main"].instance_count == 1


class TestScheduler:
    def test_selects_least_loaded(self) -> None:
        pool = _pool()
        _mock_client(pool, {})
        sched = InstanceScheduler(pool)
        pool.increment("main")
        assert sched.select() == "remote-1"
        pool.increment("remote-1")
        assert sched.select() == "main"  # tie → both at 1, sorted order

    def test_explicit_host_wins(self) -> None:
        pool = _pool()
        _mock_client(pool, {})
        pool.increment("main")
        assert InstanceScheduler(pool).select("main") == "main"

    def test_explicit_unknown_raises(self) -> None:
        with pytest.raises(CarrierError):
            InstanceScheduler(_pool()).select("nope")

    def test_explicit_unreachable_raises(self) -> None:
        pool = _pool()
        _mock_client(pool, {"remote-1": False})
        with pytest.raises(CarrierError):
            InstanceScheduler(pool).select("remote-1")

    def test_all_full_raises(self) -> None:
        pool = _pool(**{"a": {"docker": "local", "max_instances": 1}})
        _mock_client(pool, {})
        sched = InstanceScheduler(pool)
        pool.increment("a")
        with pytest.raises(CarrierError):
            sched.select()

    def test_unreachable_skipped_in_auto_select(self) -> None:
        pool = _pool()
        _mock_client(pool, {"main": False, "remote-1": True})
        sched = InstanceScheduler(pool)
        assert sched.select() == "remote-1"

    def test_default_max_applies(self) -> None:
        pool = _pool(**{"a": {"docker": "local"}})  # no per-host override
        _mock_client(pool, {})
        sched = InstanceScheduler(pool, default_max=1)
        assert sched.select() == "a"
        pool.increment("a")
        with pytest.raises(CarrierError):
            sched.select()
