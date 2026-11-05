"""NetPolicy: per-host network membership isolation (DD-05 §4.1, R3).

Each execution host runs its own Docker daemon, so networks are tracked per
(host, network_name). P0: in-memory tracking only. Membership isolation
(EE-009): containers join lab-<id> + trace on their host, never mgmt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rsi.carrier.hosts import HostPool

TRACE_NET = "trace"


class NetPolicy:
    """Per-host network membership registry.

    ``_networks`` maps (host, network_name) → set of container ids.
    """

    def __init__(self, host_pool: HostPool | None = None) -> None:
        self.host_pool = host_pool
        self._networks: dict[tuple[str, str], set[str]] = {}

    def _ensure(self, host: str, name: str) -> set[str]:
        return self._networks.setdefault((host, name), set())

    def ensure_lab_net(self, host: str, lab_id: str) -> str:
        """Create/get the lab network ``lab-<lab_id>`` on the given host."""
        self._ensure(host, f"lab-{lab_id}")
        return f"lab-{lab_id}"

    def ensure_trace_net(self, host: str) -> str:
        """Create/get the 'trace' network on the given host."""
        self._ensure(host, TRACE_NET)
        return TRACE_NET

    def attach(self, host: str, container_id: str, networks: list[str]) -> None:
        """Record container membership in each named network on the host."""
        for name in networks:
            self._ensure(host, name).add(container_id)

    def detach(self, container_id: str) -> None:
        """Remove container from all networks on all hosts."""
        for members in self._networks.values():
            members.discard(container_id)

    def is_attached(self, container_id: str, network: str) -> bool:
        """Check membership in ``network`` on any host."""
        return any(
            container_id in members
            for (_host, name), members in self._networks.items()
            if name == network
        )

    def is_attached_on(self, host: str, container_id: str, network: str) -> bool:
        """Check membership in ``network`` on a specific host."""
        return container_id in self._networks.get((host, network), set())

    def egress_rules(
        self, container_id: str, allowed_subnets: list[str], ports: list[int] | None = None
    ) -> None:
        """P1: iptables whitelist egress rules. Not implemented in P0."""
        raise NotImplementedError("egress_rules is P1 scope")
