"""HostPool: multi-host Docker client pool (DD-05 §4.1, R1).

Two modes:
- offline stub (default constructor, no configs): single "main" host,
  ``client`` returns None, ``check_reach`` is True — keeps P0 offline tests
  and integration runs working without a Docker daemon.
- configured mode (explicit ``host_configs``): lazy DockerClient per host —
  ``local`` via ``docker.from_env()`` (Windows npipe) or ``ssh://user@host``
  via ``docker.DockerClient(base_url=...)``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rsi.core.errors import CarrierError
from rsi.log import get_logger

__all__ = ["HostConfig", "HostPool", "HostStatus"]

logger = get_logger(__name__)


class HostConfig(BaseModel):
    """Configuration for one Docker execution host."""

    docker: str  # "local" or "ssh://user@host"
    subnets: list[str] = Field(default_factory=list)
    max_instances: int | None = None  # per-host limit (None → scheduler default)


class HostStatus(BaseModel):
    """Snapshot for one execution host."""

    host: str
    docker_url: str
    reachable: bool
    instance_count: int
    max_instances: int


class HostPool:
    """Registry of Docker execution hosts with lazy client caching."""

    def __init__(self, host_configs: dict[str, dict] | None = None) -> None:
        self._offline = host_configs is None
        raw = host_configs if host_configs is not None else {"main": {"docker": "local"}}
        self._configs: dict[str, HostConfig] = {
            key: HostConfig(**cfg) for key, cfg in raw.items()
        }
        self._clients: dict[str, Any] = {}
        self._instance_counts: dict[str, int] = {key: 0 for key in self._configs}

    def config(self, host: str) -> HostConfig:
        cfg = self._configs.get(host)
        if cfg is None:
            raise CarrierError(f"host not configured: {host}")
        return cfg

    def client(self, host: str) -> Any:
        """Get or create (and cache) the DockerClient for host."""
        logger.debug("client.entry", host=host, offline=self._offline)
        if host not in self._configs:
            logger.debug("client.error", exc_type="CarrierError", host=host)
            raise CarrierError(f"host not configured: {host}")
        if self._offline:
            return None
        if host not in self._clients:
            import docker

            url = self._configs[host].docker
            if url == "local":
                self._clients[host] = docker.from_env()
            elif url.startswith("ssh://") or url.startswith("tcp://"):
                self._clients[host] = docker.DockerClient(base_url=url)
            else:
                logger.debug("client.error", exc_type="CarrierError", host=host, url=url)
                raise CarrierError(f"unsupported docker URL for host {host}: {url}")
        return self._clients[host]

    def check_reach(self, host: str) -> bool:
        """Ping the host daemon; True/False. Offline stub: True."""
        logger.debug("check_reach.entry", host=host)
        if host not in self._configs:
            logger.debug("check_reach.exit", host=host, reachable=False, reason="not_configured")
            return False
        if self._offline:
            logger.debug("check_reach.exit", host=host, reachable=True, reason="offline_stub")
            return True
        try:
            reachable = bool(self.client(host).ping())
        except Exception as exc:
            logger.debug("check_reach.error", host=host, exc_type=type(exc).__name__)
            return False
        logger.debug("check_reach.exit", host=host, reachable=reachable)
        return reachable

    def statuses(self) -> list[HostStatus]:
        """All hosts with reachability and instance-load snapshot."""
        logger.debug("statuses.entry", hosts=len(self._configs))
        result = [
            HostStatus(
                host=host,
                docker_url=cfg.docker,
                reachable=self.check_reach(host),
                instance_count=self._instance_counts.get(host, 0),
                max_instances=cfg.max_instances or 0,
            )
            for host, cfg in self._configs.items()
        ]
        logger.debug("statuses.exit", count=len(result))
        return result

    def max_instances(self, host: str) -> int | None:
        """Per-host max override, or None if unset (use global default)."""
        cfg = self._configs.get(host)
        return cfg.max_instances if cfg else None

    def increment(self, host: str) -> None:
        if host not in self._instance_counts:
            raise CarrierError(f"host not configured: {host}")
        self._instance_counts[host] += 1

    def decrement(self, host: str) -> None:
        if host not in self._instance_counts:
            raise CarrierError(f"host not configured: {host}")
        self._instance_counts[host] = max(0, self._instance_counts[host] - 1)
