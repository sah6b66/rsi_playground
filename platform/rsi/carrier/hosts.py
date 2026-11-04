"""HostPool: execution host registry (DD-05 §4).

P0 offline stub — only the local "main" host exists; ``client`` returns None
(a real DockerClient via ``docker.from_env()`` npipe lands in a later phase).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class HostStatus(BaseModel):
    """Reachability snapshot for one execution host."""

    host: str
    reachable: bool
    detail: str


class HostPool:
    """Registry of Docker execution hosts. P0: single offline "main" host."""

    def __init__(self) -> None:
        self._hosts: dict[str, HostStatus] = {
            "main": HostStatus(host="main", reachable=True, detail="local npipe (P0 offline stub)")
        }

    def client(self, host: str) -> Any:
        """Docker client for host. P0 offline: returns None."""
        return None

    def check_reach(self, host: str) -> bool:
        """P0 offline: True for 'main', False for unknown hosts."""
        status = self._hosts.get(host)
        return status.reachable if status else False

    def statuses(self) -> list[HostStatus]:
        """All hosts with reach status (/sandbox/backends carrier part)."""
        return list(self._hosts.values())
