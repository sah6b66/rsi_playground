"""Unit tests: carrier HostPool + NetPolicy (DD-05 §3-4, P0 offline)."""

from __future__ import annotations

import pytest

from rsi.carrier.hosts import HostPool, HostStatus
from rsi.carrier.network import NetPolicy


class TestHostPool:
    def test_default_main_host(self) -> None:
        pool = HostPool()
        statuses = pool.statuses()
        assert [s.host for s in statuses] == ["main"]
        assert statuses[0].reachable is True
        assert isinstance(statuses[0], HostStatus)

    def test_client_p0_returns_none(self) -> None:
        pool = HostPool()
        assert pool.client("main") is None

    def test_check_reach_main_true(self) -> None:
        pool = HostPool()
        assert pool.check_reach("main") is True

    def test_check_reach_unknown_host_false(self) -> None:
        pool = HostPool()
        assert pool.check_reach("remote-1") is False

    def test_statuses_lists_all_hosts(self) -> None:
        pool = HostPool()
        pool._hosts["remote-1"] = HostStatus(
            host="remote-1", reachable=False, detail="offline stub"
        )
        names = {s.host for s in pool.statuses()}
        assert names == {"main", "remote-1"}


class TestNetPolicyNetworks:
    def test_ensure_lab_net_name(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_lab_net("A") == "lab-A"
        assert "lab-A" in pol._networks

    def test_ensure_lab_net_idempotent(self) -> None:
        pol = NetPolicy()
        n1 = pol.ensure_lab_net("A")
        n2 = pol.ensure_lab_net("A")
        assert n1 == n2 == "lab-A"

    def test_ensure_trace_net_singleton(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_trace_net() == "trace"
        assert pol.ensure_trace_net() == "trace"

    def test_attach_records_membership(self) -> None:
        pol = NetPolicy()
        pol.ensure_lab_net("A")
        pol.attach("c1", ["lab-A", "trace"])
        assert pol.is_attached("c1", "lab-A")
        assert pol.is_attached("c1", "trace")

    def test_attach_unknown_network_creates_it(self) -> None:
        pol = NetPolicy()
        pol.attach("c1", ["lab-B"])
        assert pol.is_attached("c1", "lab-B")

    def test_lifecycle_detach(self) -> None:
        pol = NetPolicy()
        pol.attach("c1", ["lab-A", "trace"])
        pol.attach("c2", ["lab-A"])
        pol.detach("c1")
        assert not pol.is_attached("c1", "lab-A")
        assert not pol.is_attached("c1", "trace")
        assert pol.is_attached("c2", "lab-A")

    def test_lab_isolation(self) -> None:
        pol = NetPolicy()
        pol.attach("cA", [pol.ensure_lab_net("A")])
        pol.attach("cB", [pol.ensure_lab_net("B")])
        assert pol.is_attached("cA", "lab-A")
        assert not pol.is_attached("cA", "lab-B")
        assert pol.is_attached("cB", "lab-B")
        assert not pol.is_attached("cB", "lab-A")

    def test_detach_unknown_container_noop(self) -> None:
        pol = NetPolicy()
        pol.detach("nope")

    def test_egress_rules_p1_not_implemented(self) -> None:
        pol = NetPolicy()
        with pytest.raises(NotImplementedError):
            pol.egress_rules("c1", ["10.0.0.0/8"])
