"""InstanceScheduler: new-instance host selection (DD-05 §4.1, R2).

Least-loaded first, bounded by per-host max_instances. Reachability is a
preference in auto-select (offline dev hosts without a daemon are still
usable), but an explicit host must be configured, reachable and under limit.
"""

from __future__ import annotations

from rsi.carrier.hosts import HostPool
from rsi.core.errors import CarrierError

__all__ = ["InstanceScheduler"]


class InstanceScheduler:
    """Select the host for a new SUT instance."""

    def __init__(self, host_pool: HostPool, default_max: int = 10) -> None:
        self.host_pool = host_pool
        self.default_max = default_max

    def _limit(self, host: str) -> int:
        per_host = self.host_pool.max_instances(host)
        return per_host if per_host else self.default_max

    def _count(self, host: str) -> int:
        return self.host_pool._instance_counts.get(host, 0)

    def _candidates(self) -> list[str]:
        return sorted(self.host_pool._configs, key=self._count)

    def select(self, explicit_host: str | None = None) -> str:
        """Choose a host: explicit (validated) or least-loaded auto-select."""
        if explicit_host is not None:
            if explicit_host not in self.host_pool._configs:
                raise CarrierError(f"host not configured: {explicit_host}")
            if self._count(explicit_host) >= self._limit(explicit_host):
                raise CarrierError(f"host at max_instances: {explicit_host}")
            if not self.host_pool.check_reach(explicit_host):
                raise CarrierError(f"host unreachable: {explicit_host}")
            return explicit_host

        reachable = [h for h in self._candidates() if self.host_pool.check_reach(h)]
        for host in reachable:
            if self._count(host) < self._limit(host):
                return host
        # Offline/dev fallback: no probeable daemon anywhere — still pick the
        # least-loaded configured host so the adapter-driven P0 path works.
        for host in self._candidates():
            if self._count(host) < self._limit(host):
                return host
        raise CarrierError("all hosts are at max_instances")
