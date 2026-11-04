"""SandboxBackend ABC (DD-04 §2 backends/base.py).

A backend owns lab lifecycle (build/destroy), snapshots, and synchronous
device CLI execution. Concrete adapters: ENSPPro (host SSH + pg-enspctl),
PNETLab, and the in-memory Fake used for offline tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from rsi.core.models import EnvSpec, LabHandle, SnapRef
from rsi.sandbox.readiness import ReadyReport

__all__ = ["BackendStatus", "SandboxBackend"]


class BackendStatus(BaseModel):
    """GET /sandbox/backends data source (DD-04 §4.1)."""

    key: str
    kind: str  # "fake" | "ensp-pro" | "pnetlab" | ...
    active_labs: int = 0
    available: bool = True
    detail: str = ""


class SandboxBackend(ABC):
    """Abstract sandbox backend (DD-04 §4/§5.6)."""

    #: backend kind, must match EnvSpec.backend values ("fake", ...)
    kind: str = ""

    @abstractmethod
    async def build(self, env: EnvSpec) -> LabHandle:
        """Create a lab from an EnvSpec; returns resolved endpoints."""

    @abstractmethod
    async def wait_ready(self, lab: LabHandle, timeout_s: int = 600) -> ReadyReport:
        """Block until the lab is ready or timeout; returns a report."""

    @abstractmethod
    async def snapshot(self, lab: LabHandle, name: str) -> SnapRef:
        """Capture lab state (native or degraded copy)."""

    @abstractmethod
    async def restore(self, lab: LabHandle, snap: SnapRef) -> None:
        """Restore lab state from a snapshot."""

    @abstractmethod
    async def reset(self, lab: LabHandle) -> None:
        """Reset to build-time state."""

    @abstractmethod
    async def destroy(self, lab: LabHandle) -> None:
        """Tear down the lab and reclaim resources."""

    @abstractmethod
    def exec(self, lab: LabHandle, device: str, cli: str) -> str:
        """Execute one CLI command on a lab device; returns raw output.

        Unknown device or lost lab raises EnvError; command-level errors
        surface as "Error: ..." output (judge layer's concern). ``lab`` is
        required because device names are only unique within a lab.
        """

    async def apply_fault(self, lab: LabHandle, fault: Any) -> None:
        """Fault-injection stub (P1 — no fault compile layer per v2.8)."""
        return

    @property
    @abstractmethod
    def active_lab_count(self) -> int:
        """Number of currently live labs (LabScheduler input, ADR-24)."""
