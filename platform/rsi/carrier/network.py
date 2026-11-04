"""NetPolicy: network membership isolation (DD-05 §4).

P0 in-memory tracking only — no real Docker networks are created.
Membership isolation (EE-009): containers join lab-<id> + trace, never mgmt.
"""

from __future__ import annotations

TRACE_NET = "trace"


class NetPolicy:
    """In-memory network membership registry. P0: dict[name, set[container_id]]."""

    def __init__(self) -> None:
        self._networks: dict[str, set[str]] = {}

    def _ensure(self, name: str) -> set[str]:
        return self._networks.setdefault(name, set())

    def ensure_lab_net(self, lab_id: str) -> str:
        """Create/get the lab network name ``lab-<lab_id>``."""
        self._ensure(f"lab-{lab_id}")
        return f"lab-{lab_id}"

    def ensure_trace_net(self) -> str:
        """Create/get the singleton 'trace' network."""
        self._ensure(TRACE_NET)
        return TRACE_NET

    def attach(self, container_id: str, networks: list[str]) -> None:
        """Record container membership in each named network."""
        for name in networks:
            self._ensure(name).add(container_id)

    def detach(self, container_id: str) -> None:
        """Remove container from all networks."""
        for members in self._networks.values():
            members.discard(container_id)

    def is_attached(self, container_id: str, network: str) -> bool:
        """Check whether a container is a member of a network."""
        return container_id in self._networks.get(network, set())

    def egress_rules(
        self, container_id: str, allowed_subnets: list[str], ports: list[int] | None = None
    ) -> None:
        """P1: iptables whitelist egress rules. Not implemented in P0."""
        raise NotImplementedError("egress_rules is P1 scope")
