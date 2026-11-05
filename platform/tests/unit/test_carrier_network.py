"""Unit tests: carrier HostPool offline stub + NetPolicy basics (DD-05 §4)."""

from __future__ import annotations

import pytest

from rsi.carrier.hosts import HostPool, HostStatus
from rsi.carrier.network import NetPolicy


class TestHostPoolOfflineStub:
    def test_default_main_host(self) -> None:
        pool = HostPool()
        statuses = pool.statuses()
        assert [s.host for s in statuses] == ["main"]
        assert statuses[0].reachable is True
        assert isinstance(statuses[0], HostStatus)
        assert statuses[0].docker_url == "local"

    def test_client_offline_returns_none(self) -> None:
        assert HostPool().client("main") is None

    def test_check_reach_main_true(self) -> None:
        assert HostPool().check_reach("main") is True

    def test_check_reach_unknown_host_false(self) -> None:
        assert HostPool().check_reach("remote-1") is False


class TestNetPolicyNetworks:
    def test_ensure_lab_net_name(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_lab_net("main", "A") == "lab-A"
        assert ("main", "lab-A") in pol._networks

    def test_ensure_lab_net_idempotent(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_lab_net("main", "A") == pol.ensure_lab_net("main", "A")

    def test_ensure_trace_net_singleton(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_trace_net("main") == "trace"
        assert pol.ensure_trace_net("main") == "trace"

    def test_attach_records_membership(self) -> None:
        pol = NetPolicy()
        pol.ensure_lab_net("main", "A")
        pol.attach("main", "c1", ["lab-A", "trace"])
        assert pol.is_attached("c1", "lab-A")
        assert pol.is_attached("c1", "trace")

    def test_attach_unknown_network_creates_it(self) -> None:
        pol = NetPolicy()
        pol.attach("main", "c1", ["lab-B"])
        assert pol.is_attached("c1", "lab-B")

    def test_lifecycle_detach(self) -> None:
        pol = NetPolicy()
        pol.attach("main", "c1", ["lab-A", "trace"])
        pol.attach("main", "c2", ["lab-A"])
        pol.detach("c1")
        assert not pol.is_attached("c1", "lab-A")
        assert not pol.is_attached("c1", "trace")
        assert pol.is_attached("c2", "lab-A")

    def test_lab_isolation(self) -> None:
        pol = NetPolicy()
        pol.attach("main", "cA", [pol.ensure_lab_net("main", "A")])
        pol.attach("main", "cB", [pol.ensure_lab_net("main", "B")])
        assert pol.is_attached("cA", "lab-A")
        assert not pol.is_attached("cA", "lab-B")
        assert pol.is_attached("cB", "lab-B")
        assert not pol.is_attached("cB", "lab-A")

    def test_egress_rules_p1_not_implemented(self) -> None:
        pol = NetPolicy()
        with pytest.raises(NotImplementedError):
            pol.egress_rules("c1", ["10.0.0.0/8"])
