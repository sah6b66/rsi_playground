"""Unit tests: NetPolicy per-host networks (DD-05 §4.1, R3)."""

from __future__ import annotations

import pytest

from rsi.carrier.network import NetPolicy


class TestPerHostNetworks:
    def test_lab_net_per_host_separate(self) -> None:
        pol = NetPolicy()
        a = pol.ensure_lab_net("host-a", "L1")
        b = pol.ensure_lab_net("host-b", "L1")
        assert a == b == "lab-L1"  # same name…
        assert ("host-a", "lab-L1") in pol._networks  # …distinct per-host nets
        assert ("host-b", "lab-L1") in pol._networks

    def test_trace_net_per_host(self) -> None:
        pol = NetPolicy()
        assert pol.ensure_trace_net("host-a") == "trace"
        assert pol.ensure_trace_net("host-b") == "trace"
        assert ("host-a", "trace") in pol._networks
        assert ("host-b", "trace") in pol._networks

    def test_attach_with_host_dimension(self) -> None:
        pol = NetPolicy()
        pol.attach("host-a", "c1", [pol.ensure_lab_net("host-a", "L1")])
        assert pol.is_attached_on("host-a", "c1", "lab-L1")
        assert not pol.is_attached_on("host-b", "c1", "lab-L1")

    def test_cross_host_isolation(self) -> None:
        pol = NetPolicy()
        pol.attach("host-a", "cA", [pol.ensure_lab_net("host-a", "L1")])
        pol.attach("host-b", "cB", [pol.ensure_lab_net("host-b", "L1")])
        # host-a's lab-L1 members are not visible on host-b's lab-L1
        assert pol.is_attached_on("host-a", "cA", "lab-L1")
        assert not pol.is_attached_on("host-b", "cA", "lab-L1")
        assert pol.is_attached_on("host-b", "cB", "lab-L1")
        assert not pol.is_attached_on("host-a", "cB", "lab-L1")

    def test_is_attached_any_host(self) -> None:
        pol = NetPolicy()
        pol.attach("host-b", "c1", ["lab-L1"])
        assert pol.is_attached("c1", "lab-L1")

    def test_detach_removes_from_all_hosts(self) -> None:
        pol = NetPolicy()
        pol.attach("host-a", "c1", ["lab-L1", "trace"])
        pol.attach("host-b", "c1", ["trace"])
        pol.detach("c1")
        assert not pol.is_attached("c1", "lab-L1")
        assert not pol.is_attached("c1", "trace")
        assert not pol.is_attached_on("host-a", "c1", "trace")

    def test_detach_unknown_noop(self) -> None:
        NetPolicy().detach("nope")

    def test_egress_rules_p1(self) -> None:
        with pytest.raises(NotImplementedError):
            NetPolicy().egress_rules("c1", ["10.0.0.0/8"])
